# NetAgent-IA — Proyecto 2, Grupo 6 (Redes Programables, UTN)

Agente de IA que administra una topología MikroTik simulada en GNS3 mediante un
catálogo cerrado de herramientas, aprobación humana y verificación posterior.

> **Estado actual:** el agente y dos herramientas Python están implementados.
> Ollama local es el proveedor predeterminado; OpenAI sigue disponible. Los
> playbooks Ansible de bloqueo y su integración están pendientes.

---

## 1. Topología de referencia

| Equipo | Rol | Dirección |
|---|---|---|
| R1 | Router MikroTik (borde LAN/WAN) | 10.60.30.202 |
| SW1 | Switch MikroTik (acceso) | 10.60.30.204 |
| PC1 | Cliente LAN | 192.168.10.50 |
| PC2 | Cliente LAN | 192.168.10.60 |
| Servidor-WAN | Servidor de la WAN simulada | 172.16.100.10 |

* Gateway LAN: `192.168.10.1`
* Gateway WAN simulada: `172.16.100.1`
* Red de gestión (tap de GNS3): `10.60.30.0/24`

Estos datos viven **solo** en `config/dispositivos.yml`; el inventario de Ansible
y los scripts se derivan de ahí para no duplicar direcciones.

---

## 2. Estructura del proyecto

```
P2_Final_Grupo6/
├── README.md                  Este documento
├── requirements.txt           Dependencias Python fijadas y verificadas
├── .env.example               Plantilla de variables de entorno (sin secretos)
├── .gitignore                 Excluye .env, .venv y registros de sesión
├── .venv/                     Entorno virtual local (no versionado)
│
├── config/
│   ├── __init__.py
│   ├── ajustes.py             Carga y validación central de la configuración
│   └── dispositivos.yml       Inventario lógico, catálogo de interfaces y
│                              política de bloqueo (sin credenciales)
│
├── agente/                    Agente con function calling y catálogo cerrado
│   ├── catalogo.py, politicas.py, planificador.py, ejecutar.py
│   └── interpretar.py, cerebro.py
│
├── herramientas/              (Etapa 2) herramientas Python del catálogo
│   ├── __init__.py
│   ├── comun.py               Sesión SSH Netmiko, catálogo cerrado y sobre JSON
│   ├── estado_interfaces.py   Consulta de solo lectura de interfaces (r1/sw1)
│   └── actualizar_comentario.py  Cambio de comentario con confirmación
│
├── ansible/
│   ├── ansible.cfg            Configuración de Ansible para el laboratorio
│   ├── inventario.ini         Inventario GENERADO desde dispositivos.yml
│   └── group_vars/
│       └── mikrotik.yml       Conexión RouterOS y credenciales desde el entorno
│
├── scripts/
│   ├── __init__.py
│   ├── activar_entorno.sh     Exporta .env y activa el venv
│   ├── generar_inventario.py  Genera/valida ansible/inventario.ini
│   └── verificar_entorno.py   Diagnóstico del entorno (solo lectura, JSON)
│
├── tests/
│   ├── __init__.py
│   ├── test_comun.py          Pruebas de la capa común (SSH simulado)
│   └── test_estado_interfaces.py  Pruebas del parser y de las respuestas JSON
│
└── registros/                 Informes JSON y registros de sesión
    └── .gitkeep
```

### Qué hace cada archivo

**`config/dispositivos.yml`** — única fuente de verdad del laboratorio. Define
los dispositivos administrables (clave, nombre, IP, rol, plataforma, prefijo de
variables de entorno), el catálogo cerrado de interfaces modificables y la
política de bloqueo: redes IP autorizadas e infraestructura que nunca puede
bloquearse. No contiene credenciales.

**`config/ajustes.py`** — API de configuración del proyecto. Lee el YAML, carga
`.env` (sin sobrescribir variables ya exportadas) y entrega objetos inmutables
(`Dispositivo`, `Credenciales`, `PoliticaBloqueo`, `Ajustes`) ya validados.
Concentra las reglas que no dependen de la IA:

* `Ajustes.dispositivo(clave)` — equipo por clave, error si no existe.
* `Ajustes.credenciales(clave)` — usuario/contraseña desde el entorno; error
  indicando qué variable falta si no está configurada.
* `Ajustes.variables_entorno_faltantes()` — nombres de variables pendientes
  (nunca valores).
* `PoliticaBloqueo.validar_objetivo(ip)` — acepta solo IPv4 dentro de las redes
  autorizadas y rechaza infraestructura protegida. Es la barrera que impedirá
  que el agente bloquee R1, SW1, gateways o el Servidor-WAN.

Todo error de configuración lanza `ErrorConfiguracion`: el sistema **falla
cerrado**, no arranca con datos dudosos. Las credenciales se ocultan en
`str()`/`repr()` para no filtrarse en logs o trazas.

**`ansible/ansible.cfg`** — inventario por defecto, rutas de colecciones y
timeouts holgados para RouterOS virtualizado. `host_key_checking = False` es una
decisión **exclusiva del laboratorio GNS3**, donde las claves SSH se regeneran en
cada arranque; no debe usarse fuera de este entorno.

**`ansible/group_vars/mikrotik.yml`** — conexión `ansible.netcommon.network_cli`
con `community.routeros.routeros`. El usuario y la contraseña se resuelven con
`lookup('env', prefijo_env ~ '_USUARIO' / '_CLAVE')`, de modo que cada host usa
su propio prefijo (`NETAGENT_R1`, `NETAGENT_SW1`) y **ningún secreto queda
escrito** en el repositorio.

**`ansible/inventario.ini`** — generado; no editar a mano. Agrupa los hosts por
`grupo_ansible` y aporta `ansible_host` y `prefijo_env`.

**`scripts/generar_inventario.py`** — deriva el inventario desde el YAML.
`--verificar` compara sin escribir y devuelve código de salida 1 si está
desactualizado (se usa en la verificación del entorno).

**`scripts/verificar_entorno.py`** — diagnóstico de solo lectura: Python,
dependencias, Ansible, colecciones, `.env`, validez de la configuración,
sincronía del inventario, parseo por `ansible-inventory` y conectividad TCP al
puerto SSH de cada equipo. Escribe `registros/verificacion_entorno.json` y
devuelve 1 solo si hay fallas (los avisos no rompen la verificación, salvo con
`--estricto`).

**`scripts/activar_entorno.sh`** — exporta las variables de `.env` (necesarias
para Ansible) y activa `.venv`.

**`herramientas/comun.py`** — capa única de acceso a los MikroTik. Abre sesiones
Netmiko con IP y credenciales de `config.ajustes`, aplica timeouts de conexión
(20 s) y lectura (45 s), cierra siempre la sesión y traduce toda falla a
`ErrorHerramienta` con un tipo estable (`autenticacion`, `tiempo_agotado`,
`conexion`, `comando`, `comando_no_autorizado`). Solo ejecuta comandos del
catálogo cerrado `COMANDOS_LECTURA`; el sobre común de respuesta lo construye
`construir_respuesta()` y se imprime con `imprimir_json()`. La contraseña se
redacta de cualquier mensaje de error.

**`herramientas/estado_interfaces.py`** — herramienta de solo lectura. Consulta
`/interface print detail` y `/ip address print detail`, y devuelve nombre,
estado (`running`, `down`, `disabled`), IP y comentario por interfaz. Acepta
solo claves presentes en `config/dispositivos.yml` (r1/sw1); nunca lanza
excepciones: informa `ok=false` con el tipo de error y una lista vacía. Si la
salida no se puede interpretar, devuelve `respuesta_no_reconocida` en lugar de
un resultado inventado. Su parser soporta RouterOS 6 y 7, comentarios `;;;`
(la forma en que `print detail` presenta el comentario de una interfaz, asociada
siempre a su propio registro y nunca a la entrada siguiente), líneas de
continuación y valores entre comillas; si la entrada no trae `;;;`, se usa la
propiedad `comment=`.

**`herramientas/actualizar_comentario.py`** — cambia **solo** la propiedad
`comment` de una interfaz incluida en `interfaces_autorizadas`. Valida
parámetros, equipo e interfaz antes de conectar; exige `--confirmacion`
(explícita, nunca por defecto); lee el comentario actual y no envía nada si ya
coincide; construye el comando RouterOS internamente (no acepta comandos
arbitrarios); y solo informa `changed: true` después de releer la interfaz y
comprobar el texto. Cierra la sesión en todos los caminos. La escritura se
envía con `send_command_timing` (lectura por tiempo) y no con `send_command`:
RouterOS repinta el eco y reimprime el prompt, así que la parada por patrón de
prompt podía devolver el control con el prompt previo a la ejecución y una
salida vacía, ocultando la respuesta real del equipo.

**`tests/`** — pruebas con `unittest` (sin dependencias nuevas). Simulan las
salidas de RouterOS y la sesión Netmiko con `unittest.mock`; no abren SSH ni
requieren GNS3. La sesión simulada de actualización sigue el guion lectura →
modificación → relectura, y también cubre fallos de conexión, de modificación y
de verificación.

**`registros/`** — evidencia de ejecución. Los archivos se ignoran en Git salvo
`.gitkeep`; el agente (Etapa 4) guardará aquí un JSON por sesión.

---

## 3. Requisitos del entorno (verificados)

| Componente | Versión comprobada |
|---|---|
| Fedora Linux | 43 (Workstation) |
| Python | 3.14.10 (`/usr/bin/python3`) |
| Netmiko | 4.8.0 |
| OpenAI SDK | 2.24.0 |
| python-dotenv | 1.2.2 |
| PyYAML | 6.0.3 |
| ansible-core | 2.18.18 |
| `ansible.netcommon` | 8.7.1 |
| `community.routeros` | 3.22.0 |

Ansible se usa desde el sistema (no por pip). Si faltara alguna colección:

```bash
ansible-galaxy collection install ansible.netcommon community.routeros
```

---

## 4. Puesta en marcha

```bash
cd ~/P2_Final_Grupo6

# 1) Entorno virtual reutilizando los paquetes ya instalados en el sistema
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -r requirements.txt   # opcional: ya están disponibles

# 2) Credenciales y API key (nunca se versionan)
cp .env.example .env
chmod 600 .env
$EDITOR .env        # completar NETAGENT_R1_* y NETAGENT_SW1_*

# 3) Inventario de Ansible derivado del YAML
python scripts/generar_inventario.py

# 4) Verificación del entorno
python scripts/verificar_entorno.py

# 5) Pruebas unitarias (sin SSH ni GNS3)
python -m unittest discover -s tests -v
```

Para trabajar en una shell con todo cargado:

```bash
source scripts/activar_entorno.sh
```

### Proveedor de IA y ejecución del agente

Con Ollama ejecutándose en el host y el modelo `qwen3:4b` ya disponible, el
proveedor predeterminado es local. Se puede iniciar con:

```bash
source .venv/bin/activate
NETAGENT_PROVEEDOR=ollama NETAGENT_MODELO=qwen3:4b python main.py
```

La URL predeterminada es `http://127.0.0.1:11434/v1`; puede cambiarse con
`NETAGENT_OLLAMA_BASE_URL`. El SDK recibe un identificador local no secreto,
por lo que Ollama no requiere `OPENAI_API_KEY`. El agente solicita desactivar
el razonamiento con `reasoning_effort=none` y añade `/no_think` al mensaje para
Qwen. Si aparece `</think>` en el contenido, descarta el texto previo; un
`<think>` sin cerrar se rechaza. La disponibilidad de estos controles depende de
la versión y del modelo. El catálogo y la confirmación de escrituras se
aplican igual con ambos proveedores.

Para Ollama se usa por defecto `NETAGENT_OLLAMA_TIMEOUT=120` segundos de
espera de red por solicitud, sin reintentos automáticos del SDK, y
`NETAGENT_OLLAMA_MAX_TOKENS=1024` como límite de salida por respuesta. Ambos
son configurables en `.env` o en el entorno. El timeout es de inactividad de
red, no un límite global de toda la sesión; un turno puede incluir varias
solicitudes al modelo. El límite de tokens también incluye llamadas a
herramientas: si la respuesta se trunca, el agente informa el error y no
ejecuta esa propuesta. Antes de repetir tras cualquier error, revise el estado
del equipo; una operación anterior podría haberse ejecutado. OpenAI conserva
sus valores originales del SDK.

En la instalación auditada, los metadatos de `qwen3:4b` anunciaban solo
pensamiento activado y la plantilla añadía `<think>` al iniciar la respuesta.
Por ello `reasoning_effort=none` y `/no_think` no garantizan que se suprima el
razonamiento; el límite y el filtrado siguen siendo necesarios. Una prueba local
de propuesta, sin ejecutar herramientas, completó con 579 tokens, pero otras
peticiones pueden necesitar un límite o tiempo diferente.

Para seleccionar OpenAI, establezca `NETAGENT_PROVEEDOR=openai` y configure
`OPENAI_API_KEY` en el entorno o `.env`. El modelo por defecto es `gpt-4o-mini`;
`NETAGENT_MODELO` permite seleccionar otro. Un `NETAGENT_MODELO` existente
en el entorno o `.env` prevalece sobre el modelo por defecto de cada proveedor:
al cambiar de proveedor, actualícelo o elimine esa asignación. Las variables
ya exportadas tienen prioridad sobre `.env`. OpenAI puede generar cargos.

Para lecturas, el agente envía al modelo un resultado JSON limitado al equipo,
los nombres y estados de las interfaces y el comentario de R1/ether4 cuando es
seguro transmitirlo. No incluye IP, credenciales ni detalles internos de error.
El modelo puede responder, proponer otra lectura o solicitar el cambio autorizado.
La consola muestra además el resultado detallado de cada lectura al operador;
los datos adicionales no se incorporan al historial enviado al modelo.
`NETAGENT_MAX_HERRAMIENTAS` limita las llamadas por solicitud (por defecto 6),
independientemente de `NETAGENT_MAX_PASOS`, que limita mensajes por sesión.
Toda escritura requiere un diagnóstico previo real del equipo en la misma
solicitud. Si el modelo propone escribir primero, el agente realiza una lectura
y devuelve el diagnóstico para que el modelo vuelva a decidir. Si el comentario
ya coincide, no escribe; en caso contrario muestra los valores anterior y nuevo
y requiere aprobación explícita. Después de ejecutar, una segunda lectura
independiente confirma el resultado. El límite reserva espacio para la escritura
y su verificación; una falla se informa como estado incierto. Solo R1/ether4
admite cambios y no hay integración de los playbooks Ansible todavía.

Para una demostración supervisada, solicite por ejemplo `Cambia el comentario
de ether4 de R1 a P2-NetAgent-IA-Demo`, revise la vista previa y responda `s`
solo si autoriza ese cambio; `n` lo cancela. Si ya coincide no se pregunta ni
se modifica. Las lecturas y escrituras reales requieren las credenciales SSH.

### Lectura del resultado

* `[OK]` — comprobación superada.
* `[AVISO]` — pendiente de configuración o laboratorio apagado; no impide
  continuar con el desarrollo.
* `[FALLA]` — problema real (dependencia, colección, configuración o inventario
  desincronizado); debe corregirse antes de avanzar de etapa.

Estado de la última ejecución: **12 OK, 4 avisos, 0 fallas**. Los avisos son
esperables en esta etapa: todavía no existe `.env` con credenciales y la
topología GNS3 estaba detenida (los equipos no responden en el puerto 22).

---

## 5. Decisiones de seguridad ya implementadas

1. **Credenciales fuera del código**: solo en `.env`, cargadas con
   `python-dotenv`; `.env` está en `.gitignore` y se recomienda `chmod 600`.
2. **Inventario sin secretos**: Ansible resuelve usuario/contraseña desde el
   entorno en tiempo de ejecución; el inventario generado no los contiene.
3. **Configuración validada y falla cerrada** (`ErrorConfiguracion`): hosts IPv4
   válidos y no duplicados, plataformas autorizadas, catálogo de interfaces sin
   duplicados, política de bloqueo no vacía.
4. **Política de bloqueo en la configuración, no en el prompt**: el agente no
   decide qué IPs son protegidas; `validar_objetivo()` lo impone.
5. **Catálogo de interfaces cerrado**: `actualizar_comentario.py` solo acepta
   interfaces listadas en `config/dispositivos.yml`. Hoy R1 autoriza únicamente
   `ether4` (demostración); SW1 mantiene la lista vacía y, por tanto, rechaza
   cualquier cambio. Una interfaz fuera del catálogo no abre conexión.
6. **Verificación de solo lectura**: no se envía ni un comando a los equipos ni
   se modifica GNS3; la comprobación de red es una conexión TCP.

---

## 6. Etapa 2 — Herramientas implementadas

### Consulta de interfaces

```bash
python herramientas/estado_interfaces.py r1
python herramientas/estado_interfaces.py sw1
```

Salida (único objeto JSON en stdout, código de salida 0):

```json
{
  "ok": true,
  "equipo": "R1",
  "interfaces": [
    {
      "nombre": "ether1",
      "estado": "running",
      "ip": "172.16.100.1/24",
      "comentario": "WAN simulada"
    }
  ],
  "changed": false,
  "error": null
}
```

En caso de falla devuelve el mismo sobre con `ok: false`, `interfaces: []` y
`error: {"tipo": …, "mensaje": …}`; el código de salida es 1. Tipos posibles:
`configuracion`, `dispositivo_no_autorizado`, `autenticacion`, `tiempo_agotado`,
`conexion`, `comando`, `comando_no_autorizado`, `respuesta_no_reconocida` e
`interno`.

Notas de implementación:

* El estado se deduce de las banderas `R`/`X` y de las propiedades
  `running`/`disabled`, por lo que funciona con RouterOS 6 y 7.
* Si una interfaz tiene varias IPv4 activas, se listan separadas por coma; las
  direcciones deshabilitadas se omiten y no se repiten.
* Netmiko fija el ancho de terminal a 511 columnas al iniciar sesión, así que
  las propiedades no se parten a mitad de línea; aun así el parser une líneas
  de continuación y elimina códigos ANSI.

### Actualización de comentario (con confirmación)

```bash
python herramientas/actualizar_comentario.py r1 ether4 "P2-NetAgent-IA-Demo" --confirmacion
```

Sin `--confirmacion` devuelve `confirmacion_requerida` y **no abre conexión**.
Salida de un cambio confirmado y verificado:

```json
{
  "ok": true,
  "equipo": "R1",
  "interfaz": "ether4",
  "comentario_anterior": "",
  "comentario_solicitado": "P2-NetAgent-IA-Demo",
  "changed": true,
  "confirmado": true,
  "verificacion": {
    "ok": true,
    "comentario_leido": "P2-NetAgent-IA-Demo",
    "coincide": true
  },
  "respuesta_equipo": null,
  "error": null
}
```

* `changed` solo es verdadero si la relectura posterior confirma el texto. Si no
  coincide, `ok` es falso con `verificacion_fallida`; si el cambio falla, con
  `modificacion_fallida`; en ningún caso se afirma un éxito no comprobado.
* `respuesta_equipo` es la salida que devolvió RouterOS al ejecutar el cambio,
  recortada a 300 caracteres (`null` si no dijo nada). Sirve de diagnóstico: si
  el equipo rechaza la sentencia (`failure:`, `bad command name`, `syntax
  error`, `input does not match any value`, …), el texto aparece aquí y además
  se informa `modificacion_fallida`. Un `set` cuyo `[find]` no casa nada no
  imprime nada, igual que un éxito; por eso la autoridad sigue siendo la
  relectura, nunca la ausencia de error.
* Si el comentario ya coincide, devuelve `ok: true` y `changed: false` sin
  enviar ningún comando (idempotencia).
* Solo se admite texto imprimible sin comillas dobles, barra invertida, `$` ni
  saltos de línea (máx. 200 caracteres); el nombre de interfaz se valida antes
  de construir `/interface set [find name="…"] comment="…"`.
* Errores propios: `confirmacion_requerida`, `interfaz_no_autorizada`,
  `interfaz_inexistente`, `modificacion_fallida` y `verificacion_fallida`.
* **No se ejecutó contra R1/SW1**: la demostración real queda pendiente de
  autorización explícita del operador.

### Pruebas ejecutadas

```text
python -m compileall -q herramientas tests config scripts   -> OK
python -m unittest discover -s tests -v                     -> 70 pruebas, OK
```

Cubren: estructura de los sobres JSON, catálogo cerrado (un comando como
`/system reboot` se rechaza sin tocar la sesión), credenciales tomadas del
entorno y no del código, autenticación, timeouts, cierre de sesión ante
excepciones, redacción de la contraseña, equipos e interfaces no autorizados,
parser RouterOS 6/7 (incluida la asociación del comentario `;;;` a su registro,
con y sin comentario, con espacios y en el formato `comment=`), construcción
segura del comando de comentario, rechazo de inyección, falta de confirmación,
idempotencia, fallo de modificación y fallo de verificación. La escritura se comprueba con un doble que falla si llega por
`send_command`: se verifica el uso de `send_command_timing`, el orden lectura →
escritura → relectura, la detección de rechazos de RouterOS, el recorte de
`respuesta_equipo` y el cierre de la sesión en todos los caminos.

### Compatibilidad Netmiko / Python 3.14.10

* Netmiko 4.8.0 importa correctamente en Python 3.14.10 y registra el driver
  `mikrotik_routeros` (`MikrotikRouterOsSSH`); se usa `conn_timeout`, ya que el
  constructor de Netmiko 4 no acepta `timeout`.
* En Netmiko 4.8 `ReadTimeout` **no** hereda de `NetmikoTimeoutException`; la
  herramienta captura ambas para informar `tiempo_agotado`.
* Limitación: no se pudo validar una sesión SSH real porque la topología GNS3
  estaba detenida. El comportamiento de red se probó con dobles de prueba;
  queda pendiente la prueba de integración con R1 y SW1 encendidos.

## 7. Etapas siguientes (no implementadas aún)

### Etapa 2 — Prueba de integración pendiente

* Ejecutar la demo real (R1 / `ether4` / `P2-NetAgent-IA-Demo`) solo con
  autorización del operador y la topología GNS3 encendida.
* Confirmar con `estado_interfaces.py` que el resto de las interfaces y sus IP
  no cambiaron.

### Etapa 3 — Playbooks Ansible (`ansible/`)

* `consultar_bloqueo.yml` — lectura de reglas de bloqueo en R1.
* `gestionar_bloqueo.yml` — alta/baja de bloqueos sobre IPs validadas por la
  política, sin reglas duplicadas y con verificación posterior.

### Agente (`agente/`)

* Implementados: function calling sobre el catálogo, planificación, aprobación
  de escrituras, ejecución y registro JSON.
* Pendiente: integración de los playbooks Ansible y pruebas de extremo a extremo.

### Pendientes conocidos

* Ampliar `config/dispositivos.yml` → `interfaces_autorizadas` solo cuando se
  autorice otra interfaz. Hoy R1 permite únicamente `ether4`; SW1 sigue vacío
  (falla cerrada). Los nombres reales se validan con `estado_interfaces.py`.
* Mantener `.env` (ya presente en el entorno local) fuera de Git y con
  permisos restrictivos; las pruebas unitarias no usan las credenciales reales.
* Iniciar la topología en GNS3 para las pruebas de integración.
