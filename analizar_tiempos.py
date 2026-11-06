"""Resume en qué se va el tiempo de una corrida a partir de logs/proceso.log. No toca la plataforma.

Uso:
    python analizar_tiempos.py "2026-10-26 18:16:59" "2026-10-26 20:58:31"
    python analizar_tiempos.py                      # todo el log

Imprime: registros por resultado, duración promedio/mediana/máxima por registro, los pasos que
más tiempo acumulan y los 8 registros más lentos. El log no se modifica y no se imprimen
nombres: solo NIT, que ya están en el log.
"""
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LINEA = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) \| (\w+) \| (.*)$")
FILA = re.compile(r"^Fila (\d+) \((\d+)\): ([A-Z_]{4,})$")


def parsear(texto: str):
    m = LINEA.match(texto.rstrip("\n"))
    if not m:
        return None
    t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() + int(m.group(2)) / 1000
    return t, m.group(3), m.group(4)


def normalizar(mensaje: str) -> str:
    return re.sub(r"\d+", "N", mensaje)[:70]


def main() -> None:
    desde = sys.argv[1] if len(sys.argv) > 1 else "0000"
    hasta = sys.argv[2] if len(sys.argv) > 2 else "9999"
    lineas = []
    for texto in Path("logs/proceso.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = parsear(texto)
        if p and desde <= texto[:19] <= hasta:
            lineas.append(p)
    if len(lineas) < 2:
        sys.exit("No hay líneas del log en ese rango.")

    registros = []          # (fila, nit, resultado, duracion, pasos[(gap, mensaje)])
    pasos_totales = defaultdict(float)
    pasos_cuenta = defaultdict(int)
    inicio_registro = lineas[0][0]
    pasos = []
    anterior = lineas[0][0]
    for t, nivel, mensaje in lineas[1:]:
        gap = t - anterior
        anterior = t
        clave = normalizar(mensaje)
        pasos_totales[clave] += gap
        pasos_cuenta[clave] += 1
        pasos.append((gap, mensaje))
        m = FILA.match(mensaje)
        if m:
            registros.append((m.group(1), m.group(2), m.group(3), t - inicio_registro, pasos))
            inicio_registro = t
            pasos = []

    print(f"Registros en el rango: {len(registros)} | tiempo total: {(lineas[-1][0] - lineas[0][0]) / 60:.1f} min\n")
    por_resultado = defaultdict(list)
    for _, _, res, dur, _ in registros:
        por_resultado[res].append(dur)
    print(f"{'Resultado':32} {'N':>4} {'Prom s':>8} {'Mediana':>8} {'Máx s':>7} {'Total min':>10}")
    for res, durs in sorted(por_resultado.items(), key=lambda kv: -sum(kv[1])):
        print(f"{res:32} {len(durs):>4} {statistics.mean(durs):>8.1f} {statistics.median(durs):>8.1f} "
              f"{max(durs):>7.1f} {sum(durs) / 60:>10.1f}")

    print("\nPasos que más tiempo acumulan (segundos entre la línea anterior y esta):")
    for clave, total in sorted(pasos_totales.items(), key=lambda kv: -kv[1])[:15]:
        n = pasos_cuenta[clave]
        print(f"  {total / 60:6.1f} min  ({n:>4} veces, {total / n:5.1f} s c/u)  {clave}")

    print("\nLos 8 registros más lentos:")
    for fila, nit, res, dur, ps in sorted(registros, key=lambda r: -r[3])[:8]:
        print(f"  Fila {fila} ({nit}) {res}: {dur:.0f} s")
        for gap, msg in sorted(ps, key=lambda x: -x[0])[:3]:
            print(f"      {gap:5.1f} s  {msg[:90]}")


if __name__ == "__main__":
    main()