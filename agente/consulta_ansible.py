"""Adaptadores cerrados para las operaciones Ansible de CU-02."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from config.ajustes import Ajustes, ErrorConfiguracion, obtener_ajustes
from herramientas.comun import ErrorHerramienta


RAIZ = Path(__file__).resolve().parents[1]


def _entorno_ansible() -> dict[str, str]:
    entorno = os.environ.copy()
    entorno["ANSIBLE_CONFIG"] = str(RAIZ / "ansible/ansible.cfg")
    entorno["ANSIBLE_STDOUT_CALLBACK"] = "ansible.posix.json"
    entorno["ANSIBLE_NOCOLOR"] = "1"
    return entorno


def consultar_bloqueo(
    equipo: str, ajustes: Any = None, ip: str | None = None
) -> dict[str, Any]:
    if equipo != "r1":
        raise ErrorHerramienta("dispositivo_no_autorizado", "La consulta de firewall solo permite R1")
    origen_esperado = "192.168.10.50"
    extra_vars: list[str] = []
    if ip is not None:
        ajustes = ajustes or obtener_ajustes()
        try:
            origen_esperado = str(ajustes.politica_bloqueo.validar_objetivo(ip))
        except ErrorConfiguracion as exc:
            raise ErrorHerramienta("ip_no_autorizada", str(exc)) from exc
        extra_vars = [
            "--extra-vars",
            json.dumps({"ip": origen_esperado}, ensure_ascii=False, separators=(",", ":")),
        ]
    entorno = _entorno_ansible()
    comando = [
        "ansible-playbook",
        "-i",
        str(RAIZ / "ansible/inventario.ini"),
        "--limit",
        "r1",
        *extra_vars,
        str(RAIZ / "ansible/consultar_bloqueo.yml"),
    ]
    try:
        proceso = subprocess.run(
                                 comando,
                                 cwd=RAIZ, env=entorno, capture_output=True, text=True,
                                 timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ErrorHerramienta("consulta_ansible_fallida", "No se pudo completar la consulta Ansible") from exc
    if proceso.returncode != 0:
        raise ErrorHerramienta("consulta_ansible_fallida", "La consulta Ansible falló")
    try:
        documento = json.loads(proceso.stdout)
        if not isinstance(documento, dict) or documento.get("stats", {}).get("r1", {}).get("failures") != 0:
            raise ValueError("estado inválido")
        if documento["stats"]["r1"].get("unreachable") != 0:
            raise ValueError("equipo no accesible")
        resultados = [task["hosts"]["r1"]["netagent_resultado"]
                      for play in documento["plays"] for task in play["tasks"]
                      if task.get("task", {}).get("name") == "Resultado JSON de NetAgent-IA"
                      and "netagent_resultado" in task.get("hosts", {}).get("r1", {})]
        if len(resultados) != 1:
            raise ValueError("resultado ausente o duplicado")
        dato = resultados[0]
        campos = ("bloqueo_configurado", "regla_administrada_existente", "reglas_duplicadas", "segura_para_modificar")
        if (not isinstance(dato, dict) or dato.get("ok") is not True or dato.get("changed") is not False
                or dato.get("equipo") != "R1" or dato.get("origen") != origen_esperado
                or dato.get("destino") != "172.16.100.10" or "bloqueo_efectivo" not in dato
                or dato["bloqueo_efectivo"] is not None
                or dato.get("error", "ausente") is not None
                or type(dato.get("existe")) is not bool
                or any(type(dato.get(k)) is not bool for k in campos if k != "regla_administrada_existente")
                or ("regla_administrada_existente" in dato and
                    (type(dato["regla_administrada_existente"]) is not bool or
                     dato["regla_administrada_existente"] != dato["existe"]))
                or not isinstance(dato.get("reglas"), list)
                or dato["existe"] != bool(dato["reglas"])
                or dato["reglas_duplicadas"] != (len(dato["reglas"]) > 1)):
            raise ValueError("resultado inválido")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ErrorHerramienta("respuesta_ansible_invalida", "Respuesta Ansible incompleta o inválida") from exc
    return {"ok": True, "equipo": "R1", "origen": origen_esperado, "destino": "172.16.100.10",
            **{k: dato[k] for k in campos if k != "regla_administrada_existente"},
            "regla_administrada_existente": dato["existe"],
            "bloqueo_efectivo": None, "changed": False, "error": None}


def gestionar_bloqueo(
    equipo: str,
    ip: str,
    accion: str,
    confirmacion: bool = False,
    ajustes: Ajustes | None = None,
) -> dict[str, Any]:
    """Ejecuta el cambio CU-02 únicamente después de aprobación humana."""
    if not isinstance(equipo, str) or equipo.casefold() != "r1":
        raise ErrorHerramienta(
            "dispositivo_no_autorizado", "La gestión de firewall solo permite R1"
        )
    if accion not in {"bloquear", "permitir"}:
        raise ErrorHerramienta(
            "argumentos_invalidos", "La acción debe ser 'bloquear' o 'permitir'"
        )

    ajustes = ajustes or obtener_ajustes()
    try:
        objetivo = str(ajustes.politica_bloqueo.validar_objetivo(ip))
    except ErrorConfiguracion as exc:
        raise ErrorHerramienta("ip_no_autorizada", str(exc)) from exc

    if confirmacion is not True:
        raise ErrorHerramienta(
            "aprobacion_requerida",
            "Toda gestión de bloqueo requiere aprobación humana explícita",
        )

    extra_vars = json.dumps(
        {"ip": objetivo, "accion": accion}, ensure_ascii=False, separators=(",", ":")
    )
    comando = [
        "ansible-playbook",
        "-i",
        str(RAIZ / "ansible/inventario.ini"),
        "--limit",
        "r1",
        "--extra-vars",
        extra_vars,
        str(RAIZ / "ansible/gestionar_bloqueo.yml"),
    ]
    try:
        proceso = subprocess.run(
            comando,
            cwd=RAIZ,
            env=_entorno_ansible(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ErrorHerramienta(
            "gestion_ansible_fallida", "No se pudo completar la gestión Ansible"
        ) from exc
    if proceso.returncode != 0:
        raise ErrorHerramienta("gestion_ansible_fallida", "La gestión Ansible falló")

    try:
        documento = json.loads(proceso.stdout)
        stats = documento.get("stats", {}).get("r1", {})
        if not isinstance(documento, dict) or stats.get("failures") != 0:
            raise ValueError("estado inválido")
        if stats.get("unreachable") != 0:
            raise ValueError("equipo no accesible")
        resultados = [
            task["hosts"]["r1"]["netagent_resultado"]
            for play in documento["plays"]
            for task in play["tasks"]
            if task.get("task", {}).get("name") == "Resultado JSON de NetAgent-IA"
            and "netagent_resultado" in task.get("hosts", {}).get("r1", {})
        ]
        if len(resultados) != 1:
            raise ValueError("resultado ausente o duplicado")
        dato = resultados[0]
        campos = ("ok", "equipo", "ip", "bloqueada", "changed", "verified", "error")
        if (
            not isinstance(dato, dict)
            or set(dato) != set(campos)
            or dato.get("ok") is not True
            or dato.get("equipo") != "R1"
            or dato.get("ip") != objetivo
            or any(type(dato.get(campo)) is not bool for campo in ("bloqueada", "changed", "verified"))
            or dato.get("error") is not None
        ):
            raise ValueError("resultado inválido")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ErrorHerramienta(
            "respuesta_ansible_invalida", "Respuesta Ansible incompleta o inválida"
        ) from exc

    return dato
