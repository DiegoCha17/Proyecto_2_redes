"""Interpretación conservadora de la salida de lectura RouterOS de CU-02."""

from ansible.errors import AnsibleFilterError

MARCA = "NetAgent-IA:CU02:PC1-WAN"
REQUERIDAS = {".id", "chain", "action", "src-address", "dst-address", "disabled", "comment"}
# Se aceptan únicamente propiedades cuyo significado se conoce. Otros campos,
# incluso vacíos, bloquean la administración hasta revisar el formato real.
OPCIONALES = {"dynamic", "invalid", "log", "log-prefix"}
FALSOS = {"false", "no"}
BOOLEANOS = FALSOS | {"true", "yes"}


def procesar_bloqueo(lineas):
    reglas = []
    actual = None
    for linea in lineas:
        if linea == "NETAGENT_RULE_BEGIN_V2" and actual is None:
            actual = {}
        elif linea == "NETAGENT_RULE_END_V2" and actual is not None:
            reglas.append(_interpretar(actual))
            actual = None
        elif linea.startswith("NETAGENT_PROP|") and actual is not None:
            partes = linea.split("|", 2)
            if len(partes) != 3 or not partes[1] or partes[1] in actual:
                raise AnsibleFilterError("Propiedad de regla incompleta o duplicada")
            actual[partes[1]] = partes[2]
        elif linea.startswith("NETAGENT_RULE|") and actual is None:
            campos = linea.split("|")
            if len(campos) != 6 or campos[5] not in BOOLEANOS:
                raise AnsibleFilterError("Regla antigua mal formada")
            reglas.append(_interpretar({"chain": campos[1], "action": campos[2],
                "src-address": campos[3], "dst-address": campos[4],
                "disabled": campos[5]}, legado=True))
        else:
            raise AnsibleFilterError("Bloque de reglas incompleto o inesperado")
    if actual is not None:
        raise AnsibleFilterError("Regla incompleta")

    duplicadas = len(reglas) > 1
    segura = len(reglas) == 1 and reglas[0]["regla_administrada_valida"] and not duplicadas
    return {"ok": True, "equipo": "R1", "origen": "192.168.10.50",
        "destino": "172.16.100.10", "identificador": MARCA,
        "regla_administrada_existente": bool(reglas),
        "bloqueo_configurado": any(r["coincide_objetivo"] and r["habilitada"] for r in reglas),
        "bloqueo_efectivo": None, "reglas_duplicadas": duplicadas,
        "segura_para_modificar": segura,
        "limitacion": "La regla configurada no demuestra bloqueo efectivo; revisar orden y tráfico.",
        "reglas": reglas, "changed": False, "error": None}


def _interpretar(p, legado=False):
    if p.get("disabled") not in BOOLEANOS:
        raise AnsibleFilterError("Estado de regla ausente o desconocido")
    coincidente = (p.get("chain") == "forward" and p.get("action") == "drop"
        and p.get("src-address") in {"192.168.10.50", "192.168.10.50/32"}
        and p.get("dst-address") in {"172.16.100.10", "172.16.100.10/32"})
    desconocidas = sorted(set(p) - REQUERIDAS - OPCIONALES)
    anomalas = [k for k in ("dynamic", "invalid", "log") if k in p and p[k] not in FALSOS]
    if p.get("log-prefix", ""):
        anomalas.append("log-prefix")
    faltantes = sorted(REQUERIDAS - set(p))
    valido = (not legado and not faltantes and not desconocidas and not anomalas
              and bool(p.get(".id")) and p.get("comment") == MARCA and coincidente)
    return {"cadena": p.get("chain", ""), "accion": p.get("action", ""),
        "origen": p.get("src-address", ""), "destino": p.get("dst-address", ""),
        "habilitada": p["disabled"] in FALSOS, "coincide_objetivo": coincidente,
        "regla_administrada_valida": valido, "inspeccion_completa": not legado,
        "propiedades_inesperadas": desconocidas, "propiedades_anomalas": anomalas,
        "propiedades_faltantes": faltantes}


class FilterModule:
    def filters(self):
        return {"netagent_procesar_bloqueo": procesar_bloqueo}
