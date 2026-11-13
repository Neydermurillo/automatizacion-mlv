import base64
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src import licencia

EQUIPO = "7F3A-91C2-0B5D-E4A8"
HOY = date(2026, 11, 11)
AHORA = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode().rstrip("=")


@pytest.fixture
def privada():
    return Ed25519PrivateKey.generate()


def _codigo(privada, *, vence=HOY + timedelta(days=100), equipo=EQUIPO, emitida=HOY):
    datos = {"v": 1, "cliente": "Cliente", "equipo": equipo, "emitida": emitida.isoformat(),
             "inicio": emitida.isoformat(), "vence": vence.isoformat()}
    cuerpo = json.dumps(datos, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64(cuerpo)}.{_b64(privada.sign(cuerpo))}"


def _evaluar(tmp_path, privada, codigo=None, *, ahora=AHORA, hoy=HOY, equipo=EQUIPO):
    ruta = tmp_path / "licencia.lic"
    if codigo is not None:
        ruta.write_text(codigo)
    return licencia.evaluar(
        hoy=hoy, ahora=ahora, ruta_licencia=ruta, ruta_uso=tmp_path / "uso.dat",
        clave=privada.public_key(), equipo=equipo,
    )


def test_sin_archivo_no_permite_grabar(tmp_path, privada):
    est = _evaluar(tmp_path, privada)
    assert est.codigo == "sin_licencia" and not est.permite_real


def test_licencia_vigente(tmp_path, privada):
    est = _evaluar(tmp_path, privada, _codigo(privada))
    assert est.codigo == "activa" and est.permite_real and est.dias_restantes == 100


def test_por_vencer_avisa_y_permite(tmp_path, privada):
    est = _evaluar(tmp_path, privada, _codigo(privada, vence=HOY + timedelta(days=10)))
    assert est.codigo == "por_vencer" and est.permite_real and est.nivel == "warn"


def test_vence_hoy_todavia_permite(tmp_path, privada):
    est = _evaluar(tmp_path, privada, _codigo(privada, vence=HOY))
    assert est.permite_real and est.dias_restantes == 0


@pytest.mark.parametrize("dias_pasados,permite", [(1, True), (2, True), (3, False)])
def test_gracia_de_dos_dias(tmp_path, privada, dias_pasados, permite):
    est = _evaluar(tmp_path, privada, _codigo(privada, vence=HOY - timedelta(days=dias_pasados),
                                              emitida=HOY - timedelta(days=200)))
    assert est.permite_real is permite
    assert est.codigo == ("gracia" if permite else "vencida")


def test_firma_alterada_se_rechaza(tmp_path, privada):
    cuerpo, firma = _codigo(privada, vence=HOY + timedelta(days=5)).split(".")
    datos = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
    datos["vence"] = "2099-01-01"
    falso = _b64(json.dumps(datos, sort_keys=True, separators=(",", ":")).encode())
    est = _evaluar(tmp_path, privada, f"{falso}.{firma}")
    assert est.codigo == "invalida" and not est.permite_real


def test_firmada_por_otra_clave_se_rechaza(tmp_path, privada):
    est = _evaluar(tmp_path, privada, _codigo(Ed25519PrivateKey.generate()))
    assert est.codigo == "invalida"


def test_licencia_de_otro_equipo(tmp_path, privada):
    est = _evaluar(tmp_path, privada, _codigo(privada, equipo="AAAA-BBBB-CCCC-DDDD"))
    assert est.codigo == "otro_equipo" and not est.permite_real


def test_id_equipo_sin_guiones_ni_mayusculas_tambien_sirve(tmp_path, privada):
    est = _evaluar(tmp_path, privada, _codigo(privada, equipo=EQUIPO.lower().replace("-", "")))
    assert est.permite_real


def test_reloj_atrasado_respecto_al_ultimo_uso(tmp_path, privada):
    codigo = _codigo(privada, emitida=HOY - timedelta(days=30))
    assert _evaluar(tmp_path, privada, codigo).permite_real
    atrasado = AHORA - timedelta(days=20)
    est = _evaluar(tmp_path, privada, codigo, ahora=atrasado, hoy=HOY - timedelta(days=20))
    assert est.codigo == "reloj" and not est.permite_real


def test_reloj_antes_de_la_emision(tmp_path, privada):
    est = _evaluar(tmp_path, privada, _codigo(privada, emitida=HOY),
                   ahora=AHORA - timedelta(days=30), hoy=HOY - timedelta(days=30))
    assert est.codigo == "reloj"


def test_activar_guarda_y_valida(tmp_path, privada):
    ruta = tmp_path / "licencia.lic"
    est = licencia.activar(_codigo(privada), clave=privada.public_key(), ruta_licencia=ruta,
                           equipo=EQUIPO, hoy=HOY, ahora=AHORA, ruta_uso=tmp_path / "uso.dat")
    assert est.permite_real and ruta.exists()


def test_activar_rechaza_otro_equipo_y_vencida(tmp_path, privada):
    kw = dict(clave=privada.public_key(), ruta_licencia=tmp_path / "l.lic", equipo=EQUIPO, hoy=HOY)
    with pytest.raises(licencia.LicenciaInvalida):
        licencia.activar(_codigo(privada, equipo="AAAA-BBBB-CCCC-DDDD"), **kw)
    with pytest.raises(licencia.LicenciaInvalida):
        licencia.activar(_codigo(privada, vence=HOY - timedelta(days=10), emitida=HOY - timedelta(days=90)), **kw)
    assert not (tmp_path / "l.lic").exists()


def test_exigir_carga_real_bloquea_sin_licencia(tmp_path, privada):
    with pytest.raises(licencia.LicenciaError):
        licencia.exigir_carga_real(hoy=HOY, ahora=AHORA, ruta_licencia=tmp_path / "no.lic",
                                   ruta_uso=tmp_path / "uso.dat", clave=privada.public_key(), equipo=EQUIPO)


def test_id_equipo_tiene_formato_estable():
    a, b = licencia.id_equipo(), licencia.id_equipo()
    assert a == b and len(a) == 19 and a.count("-") == 3


def test_sin_libreria_cryptography_avisa_sin_romper(tmp_path, monkeypatch):
    def _falla(*a, **k):
        raise licencia.LibreriaFaltante

    monkeypatch.setattr(licencia, "cargar_clave_publica", _falla)
    est = licencia.evaluar(hoy=HOY, ahora=AHORA, ruta_licencia=tmp_path / "x.lic",
                           ruta_uso=tmp_path / "uso.dat", equipo=EQUIPO)
    assert est.codigo == "sin_libreria" and not est.permite_real
