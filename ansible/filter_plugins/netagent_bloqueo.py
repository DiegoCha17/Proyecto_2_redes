"""Interpretación conservadora de la salida de lectura RouterOS de CU-02."""

from ansible.errors import AnsibleFilterError

MARCA = "NetAgent-IA:CU02:PC1-WAN"
REQUERIDAS = {".id", "chain", "action", "src-address", "dst-address", "disabled", "comment"}
# Se aceptan únicamente propiedades cuyo significado se conoce. Otros campos,
# incluso vacíos, bloquean la administración hasta revisar el formato real.
OPCIONALES = {"dynamic", "invalid", "log", "log-prefix"}
FALSOS = {"false", "no"}
BOOLEANOS = FALSOS | {"true", "yes"}
MARCA_GESTION = "NETAGENT_LAB_"


def procesar_bloqueo(lineas, ip="192.168.10.50", exigir_get=False):
    reglas = []
    actual = None
    estado_get = None
    for linea in lineas:
        if linea == "NETAGENT_RULE_BEGIN_V2" and actual is None:
            actual = {}
            estado_get = None
        elif linea == "NETAGENT_RULE_END_V2" and actual is not None:
            if exigir_get and estado_get is None:
                raise AnsibleFilterError("Falta lectura explícita de disabled")
            if estado_get is not None:
                identificador, estado = estado_get
                if not identificador or actual.get(".id") != identificador or estado not in BOOLEANOS:
                    raise AnsibleFilterError("Lectura explícita de disabled inválida")
                previo = actual.get("disabled")
                if previo is not None and (previo not in BOOLEANOS or (previo in FALSOS) != (estado in FALSOS)):
                    raise AnsibleFilterError("Lecturas de disabled contradictorias")
                actual["disabled"] = estado
            reglas.append(_interpretar(actual, ip=ip))
            actual = None
        elif linea.startswith("NETAGENT_DISABLED|") and actual is not None:
            partes = linea.split("|")
            if len(partes) != 3 or estado_get is not None:
                raise AnsibleFilterError("Lectura explícita de disabled duplicada o incompleta")
            estado_get = (partes[1], partes[2])
        elif linea.startswith("NETAGENT_PROP|") and actual is not None:
            partes = linea.split("|", 2)
            if len(partes) != 3 or not partes[1] or partes[1] in actual:
                raise AnsibleFilterError("Propiedad de regla incompleta o duplicada")
            actual[partes[1]] = partes[2]
        elif linea.startswith("NETAGENT_RULE|") and actual is None:
            if exigir_get:
                raise AnsibleFilterError("Falta lectura explícita de disabled")
            campos = linea.split("|")
            if len(campos) != 6 or campos[5] not in BOOLEANOS:
                raise AnsibleFilterError("Regla antigua mal formada")
            reglas.append(_interpretar({"chain": campos[1], "action": campos[2],
                "src-address": campos[3], "dst-address": campos[4],
                "disabled": campos[5]}, legado=True, ip=ip))
        else:
            raise AnsibleFilterError("Bloque de reglas incompleto o inesperado")
    if actual is not None:
        raise AnsibleFilterError("Regla incompleta")

    duplicadas = len(reglas) > 1
    if exigir_get and duplicadas:
        raise AnsibleFilterError("Reglas duplicadas")
    segura = len(reglas) == 1 and reglas[0]["regla_administrada_valida"] and not duplicadas
    return {"ok": True, "equipo": "R1", "origen": ip,
        "destino": "172.16.100.10", "identificador": MARCA_GESTION + ip,
        "regla_administrada_existente": bool(reglas),
        # Alias sin la secuencia "admin": Ansible puede redactarla en las
        # claves JSON si coincide con una credencial registrada como secreto.
        "existe": bool(reglas),
        "bloqueo_configurado": any(r["coincide_objetivo"] and r["habilitada"] for r in reglas),
        "bloqueo_efectivo": None, "reglas_duplicadas": duplicadas,
        "segura_para_modificar": segura,
        "limitacion": "La regla configurada no demuestra bloqueo efectivo; revisar orden y tráfico.",
        "reglas": reglas, "changed": False, "error": None}


def _interpretar(p, legado=False, ip="192.168.10.50"):
    if p.get("disabled") not in BOOLEANOS:
        raise AnsibleFilterError("Estado de regla ausente o desconocido")
    coincidente = (p.get("chain") == "forward" and p.get("action") == "drop"
        and p.get("src-address") in {ip, ip + "/32"}
        and p.get("dst-address") in {"172.16.100.10", "172.16.100.10/32"})
    desconocidas = sorted(set(p) - REQUERIDAS - OPCIONALES)
    anomalas = [k for k in ("dynamic", "invalid", "log") if k in p and p[k] not in FALSOS]
    if p.get("log-prefix", ""):
        anomalas.append("log-prefix")
    faltantes = sorted(REQUERIDAS - set(p))
    valido = (not legado and not faltantes and not desconocidas and not anomalas
              and bool(p.get(".id")) and p.get("comment") == MARCA_GESTION + ip and coincidente)
    return {"cadena": p.get("chain", ""), "accion": p.get("action", ""),
        "origen": p.get("src-address", ""), "destino": p.get("dst-address", ""),
        "habilitada": p["disabled"] in FALSOS, "coincide_objetivo": coincidente,
        "regla_administrada_valida": valido, "inspeccion_completa": not legado,
        "propiedades_inesperadas": desconocidas, "propiedades_anomalas": anomalas,
        "propiedades_faltantes": faltantes}


class FilterModule:
    def filters(self):
        return {
            "netagent_procesar_bloqueo": procesar_bloqueo,
            "netagent_estado_gestion": estado_gestion,
            "netagent_procesar_gestion_bloqueo": procesar_gestion_bloqueo,
        }


def estado_gestion(lineas, ip):
    """Valida la lectura completa antes de autorizar cualquier modificación."""
    lineas = list(lineas)
    if not lineas or lineas.count("NETAGENT_BEGIN_V1") != 1 or lineas.count("NETAGENT_END_V1") != 1 or lineas[-1] != "NETAGENT_END_V1":
        raise AnsibleFilterError("Marcadores de lectura inválidos")
    inicio = lineas.index("NETAGENT_BEGIN_V1")
    if any(not (linea.startswith("<") or linea.startswith(":put ")) for linea in lineas[:inicio]):
        raise AnsibleFilterError("Salida inesperada antes de la lectura")
    bloque = []
    dentro_regla = False
    ecos = ()
    comandos_actuales = tuple(
        ':foreach r in=[/ip firewall filter print as-value where comment="' + marca
        + '"] do={ :put "NETAGENT_RULE_BEGIN_V2"; :foreach k,v in=$r do={ :put ("NETAGENT_PROP|" . $k . "|" . [:tostr $v]) }; :local id ($r->".id"); :put ("NETAGENT_DISABLED|" . [:tostr $id] . "|" . [:tostr [/ip firewall filter get $id disabled]]); :put "NETAGENT_RULE_END_V2" }'
        for marca in (MARCA_GESTION + ip, MARCA)
    )
    # La respuesta vacía previamente capturada también es válida en pruebas;
    # los bloques con reglas siempre requieren la lectura explícita.
    agregado = '; :local id ($r->".id"); :put ("NETAGENT_DISABLED|" . [:tostr $id] . "|" . [:tostr [/ip firewall filter get $id disabled]])'
    comandos = comandos_actuales + tuple(comando.replace(agregado, '') for comando in comandos_actuales)
    for linea in lineas[inicio + 1:-1]:
        if not dentro_regla:
            candidatos = tuple(comando for comando in comandos if comando.startswith(linea)) if linea.startswith(':foreach r in=[') else ()
            if candidatos:
                ecos = candidatos
                continue
            if linea.startswith('<') and ecos:
                # RouterOS marca con '>' la continuación truncada en pantalla;
                # ese indicador no forma parte del comando que se hizo eco.
                fragmento = linea[1:-1] if linea.endswith('>') else linea[1:]
                candidatos = tuple(comando for comando in ecos if fragmento and fragmento in comando)
                if candidatos:
                    ecos = candidatos
                    continue
            if linea == ':put "NETAGENT_END_V1"':
                ecos = ()
                continue
        ecos = ()
        bloque.append(linea)
        if linea == 'NETAGENT_RULE_BEGIN_V2':
            dentro_regla = True
        elif linea == 'NETAGENT_RULE_END_V2':
            dentro_regla = False
    resultado = procesar_bloqueo(bloque, ip, exigir_get=True)
    if resultado["regla_administrada_existente"]:
        regla = resultado["reglas"][0]
        # La marca antigua nunca se administra mediante la herramienta actual.
        if (not resultado["segura_para_modificar"] or not regla["habilitada"]
                or f"NETAGENT_PROP|comment|{MARCA_GESTION}{ip}" not in bloque):
            raise AnsibleFilterError("Regla ambigua, duplicada o incompatible")
    return resultado["regla_administrada_existente"]


def procesar_gestion_bloqueo(lineas):
    """Parsea la salida marcada del playbook de cambio CU-02.

    El parser comparte el mismo transporte y formato de propiedades que la
    consulta de lectura, pero conserva un contrato mínimo específico para la
    operación idempotente de una IP.
    """
    reglas = []
    actual = None
    ip = None
    accion = None
    changed = None
    dentro = False
    finalizado = False

    for linea in lineas:
        if finalizado:
            raise AnsibleFilterError("Salida posterior al marcador final")
        if linea == "NETAGENT_BEGIN_V1":
            if dentro:
                raise AnsibleFilterError("Marcador de inicio duplicado")
            dentro = True
        elif linea == "NETAGENT_END_V1":
            if not dentro or actual is not None:
                raise AnsibleFilterError("Marcador final inesperado")
            dentro = False
            finalizado = True
        elif not dentro:
            # Igual que el parser de lectura: se tolera el eco previo del
            # terminal, pero no texto arbitrario antes del bloque.
            if not (linea.startswith("<") or linea.startswith(":put ")):
                raise AnsibleFilterError("Salida inesperada antes del bloque")
        elif linea == "NETAGENT_RULE_BEGIN_V2" and actual is None:
            actual = {}
        elif linea == "NETAGENT_RULE_END_V2" and actual is not None:
            reglas.append(_interpretar_gestion(actual))
            actual = None
        elif linea.startswith("NETAGENT_PROP|") and actual is not None:
            partes = linea.split("|", 2)
            if len(partes) != 3 or not partes[1] or partes[1] in actual:
                raise AnsibleFilterError("Propiedad de regla incompleta o duplicada")
            actual[partes[1]] = partes[2]
        elif linea.startswith("NETAGENT_IP|") and actual is None:
            if ip is not None:
                raise AnsibleFilterError("IP duplicada")
            ip = linea.split("|", 1)[1]
        elif linea.startswith("NETAGENT_ACTION|") and actual is None:
            if accion is not None:
                raise AnsibleFilterError("Acción duplicada")
            accion = linea.split("|", 1)[1]
        elif linea.startswith("NETAGENT_CHANGED|") and actual is None:
            if changed is not None:
                raise AnsibleFilterError("Estado changed duplicado")
            valor = linea.split("|", 1)[1].lower()
            if valor not in {"true", "false"}:
                raise AnsibleFilterError("Estado changed inválido")
            changed = valor == "true"
        else:
            raise AnsibleFilterError("Bloque de cambio incompleto o inesperado")

    if dentro or actual is not None or ip is None or accion not in {"bloquear", "permitir"}:
        raise AnsibleFilterError("Respuesta de cambio incompleta")
    if changed is None:
        raise AnsibleFilterError("Falta el estado changed")

    objetivo = [regla for regla in reglas if regla["comment"] == f"{MARCA_GESTION}{ip}"]
    bloqueada = len(objetivo) == 1 and _regla_gestion_valida(objetivo[0], ip)
    if accion == "bloquear":
        verified = bloqueada
    else:
        bloqueada = False
        verified = not objetivo

    return {
        "ok": True,
        "equipo": "R1",
        "ip": ip,
        "bloqueada": bloqueada,
        "changed": changed,
        "verified": verified,
        "error": None,
    }


def _interpretar_gestion(propiedades):
    requeridas = {".id", "chain", "action", "src-address", "dst-address", "disabled", "comment"}
    if propiedades.get("disabled") not in BOOLEANOS:
        raise AnsibleFilterError("Estado de regla ausente o desconocido")
    return {
        ".id": propiedades.get(".id", ""),
        "chain": propiedades.get("chain", ""),
        "action": propiedades.get("action", ""),
        "src-address": propiedades.get("src-address", ""),
        "dst-address": propiedades.get("dst-address", ""),
        "disabled": propiedades["disabled"],
        "comment": propiedades.get("comment", ""),
        "propiedades_faltantes": sorted(requeridas - set(propiedades)),
    }


def _regla_gestion_valida(regla, ip):
    return (
        not regla["propiedades_faltantes"]
        and bool(regla[".id"])
        and regla["chain"] == "forward"
        and regla["action"] == "drop"
        and regla["src-address"] in {ip, f"{ip}/32"}
        and regla["dst-address"] in {"172.16.100.10", "172.16.100.10/32"}
        and regla["disabled"] in FALSOS
        and regla["comment"].startswith(MARCA_GESTION)
    )
