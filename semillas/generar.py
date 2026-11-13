"""Genera módulos de metodología para bug bounty autorizado (HackerOne y similares),
en el formato que Manager importa. Cada tarjeta se responde sola, sin LLM.

    python semillas/generar.py                      # escribe los .modulo.json
    python semillas/generar.py --validar            # además los importa a una base temporal y cuenta
    python semillas/generar.py --cargar http://127.0.0.1:8765   # los sube a un Manager en marcha

El conocimiento es metodología de testing autorizado: dónde mirar y cómo confirmar cada
clase de falla, siempre dentro del scope del programa. No incluye exploits contra objetivos reales.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORMAT = "manager-modulo/1"

# (pregunta, respuesta, tipo, [etiquetas], [variantes])
MODULOS: list[tuple[str, str, list]] = [
 ("Metodología y alcance (HackerOne)",
  "Cómo encarar un programa sin salirte del alcance ni romper las reglas.", [
  ("¿Qué es el scope de un programa de bug bounty?",
   "El scope define qué activos (dominios, APIs, apps móviles) podés testear y cuáles no. Está en la Policy del programa, en las secciones in-scope y out-of-scope. Todo lo que no figure como in-scope se trata como fuera de alcance.",
   "definicion", ["scope", "hackerone", "reglas"], ["¿Qué puedo testear en un programa?", "¿Dónde dice qué está permitido atacar?"]),
  ("¿Puedo testear un subdominio que encontré pero no está listado en el scope?",
   "No. Testear fuera de alcance viola las reglas del programa, puede ser ilegal y no se paga. Si el scope incluye un wildcard como *.ejemplo.com, los subdominios entran; si solo figura app.ejemplo.com, limitate a ese host. Ante la duda, no toques y preguntá por el canal del programa.",
   "advertencia", ["scope", "fuera-de-alcance"], ["¿Testeo un subdominio que no está en el scope?", "Encontré un host nuevo, ¿puedo atacarlo?"]),
  ("¿Qué técnicas suelen estar prohibidas aunque el target esté in-scope?",
   "DoS o stress testing, fuerza bruta agresiva, spam a usuarios reales, ingeniería social al personal, y automatización que degrade el servicio. Leé las secciones de out-of-scope y rules de la Policy antes de empezar: cada programa pone sus límites.",
   "advertencia", ["reglas", "prohibido"], ["¿Qué no puedo hacer aunque el host esté permitido?", "¿Está permitido hacer fuerza bruta?"]),
  ("¿Cómo encaro un target nuevo?",
   "1) Leer la Policy y anotar scope, exclusiones y recompensas. 2) Mapear la superficie (subdominios, hosts, APIs) solo dentro del scope. 3) Usar la app como usuario normal para entenderla. 4) Elegir clases de bug según cómo es la app. 5) Probar con cuentas propias de test. 6) Documentar todo desde el principio.",
   "procedimiento", ["metodologia", "recon"], ["¿Por dónde empiezo con un objetivo nuevo?", "¿Cuál es el flujo para atacar un programa?"]),
  ("¿Qué miro antes de lanzar requests automatizados?",
   "Que el host esté in-scope, limitá la tasa de requests, usá un User-Agent identificable si el programa lo pide, y evitá endpoints de pago o de producción sensibles. Preferí Burp manual sobre escaneo masivo: muchos programas penalizan el ruido.",
   "procedimiento", ["automatizacion", "rate-limit"], ["¿Puedo correr un escáner contra el target?", "¿Qué cuido antes de automatizar?"]),
  ("¿Qué información conviene guardar de cada target?",
   "El scope exacto, las credenciales de prueba que creaste, los endpoints interesantes, qué ya probaste y con qué resultado, y capturas. Así no repetís trabajo ni te salís del alcance sin querer.",
   "hecho", ["notas", "organizacion"], ["¿Qué anoto de cada programa?", "¿Qué registro mientras testeo?"]),
  ("¿Conviene empezar por programas públicos o privados?",
   "Los privados suelen tener menos cazadores y más superficie sin tocar, pero requieren invitación y buena reputación. Los públicos tienen más competencia y más duplicados. Para aprender, elegí programas con scope amplio y triage con buena fama.",
   "decision", ["estrategia"], ["¿Público o privado para arrancar?", "¿Qué tipo de programa elijo?"]),
 ]),

 ("Control de acceso (IDOR / BOLA)",
  "Acceder a datos o acciones de otros usuarios por fallas de autorización.", [
  ("¿Qué es un IDOR?",
   "Insecure Direct Object Reference: la app usa un identificador (id numérico, uuid, nombre) para acceder a un objeto sin verificar que el usuario tenga permiso sobre él. Cambiando el id accedés a datos de otro usuario. En APIs se lo llama BOLA y es la falla número uno del OWASP API Top 10.",
   "definicion", ["idor", "bola", "autorizacion"], ["¿Qué es un BOLA?", "¿Qué significa referencia directa a objeto insegura?"]),
  ("¿Cómo pruebo IDOR en una API REST?",
   "Creá dos cuentas, A y B. Con A, capturá un request que acceda a un recurso tuyo (ej. GET /api/orders/123). Cambiá el id por uno de B, o incrementá y decrementá. Si te devuelve datos de B, es IDOR. Probá también PUT y DELETE, no solo lecturas.",
   "procedimiento", ["idor", "api"], ["¿Cómo testeo IDOR?", "¿Cómo busco acceso a datos ajenos por id?"]),
  ("¿Dónde busco los identificadores para probar IDOR?",
   "En la URL, parámetros de query, body JSON, headers personalizados y cookies. Mirá ids numéricos secuenciales, UUIDs que se filtran en otras respuestas, nombres de usuario y hashes predecibles.",
   "procedimiento", ["idor", "parametros"], ["¿Qué parámetros pruebo para IDOR?", "¿Dónde están los ids que puedo cambiar?"]),
  ("¿Un UUID aleatorio descarta el IDOR?",
   "No del todo. Si el UUID se filtra en otro endpoint (listados, búsquedas, mensajes, metadatos), igual podés obtenerlo y usarlo. Buscá dónde la app expone ids de otros usuarios antes de descartar la falla.",
   "advertencia", ["idor", "uuid"], ["¿Si usa UUID no hay IDOR?", "¿El identificador aleatorio me frena?"]),
  ("¿Cómo distingo escalada horizontal de vertical?",
   "Horizontal: un usuario accede a datos de otro usuario del mismo rol (A ve lo de B). Vertical: un usuario común accede a funciones de administrador. Para la vertical, probá endpoints de admin con tu token normal y quitando o bajando parámetros de rol.",
   "definicion", ["escalada", "privilegios"], ["¿Qué es escalada horizontal y vertical?", "¿Cómo pruebo llegar a funciones de admin?"]),
  ("¿Qué hace fuerte un reporte de IDOR?",
   "Mostrar impacto real: datos sensibles de otra cuenta (PII, pagos) o una acción destructiva. Usá siempre dos cuentas tuyas, nunca datos de usuarios reales. Documentá el request y la respuesta con el id cambiado.",
   "hecho", ["idor", "reporte", "impacto"], ["¿Cómo reporto bien un IDOR?", "¿Qué pruebo para que valga el IDOR?"]),
  ("¿Qué error común hay al creer que encontré un IDOR?",
   "Confundir ver tu propio dato por otro camino con un IDOR real. Confirmá que el objeto pertenece a una cuenta distinta que vos también controlás antes de reportar.",
   "advertencia", ["idor", "falso-positivo"], ["¿Cómo evito un falso positivo de IDOR?", "¿Es IDOR si veo mi propio dato?"]),
 ]),

 ("SSRF",
  "Hacer que el servidor pida recursos que vos elegís: servicios internos, metadata de cloud.", [
  ("¿Qué es un SSRF?",
   "Server-Side Request Forgery: lográs que el servidor haga una petición (HTTP u otro protocolo) a un destino que vos controlás. Sirve para alcanzar servicios internos, la metadata de la nube o puertos internos que desde afuera no se ven.",
   "definicion", ["ssrf"], ["¿Qué significa SSRF?", "¿Qué es falsificación de petición del lado del servidor?"]),
  ("¿Dónde busco SSRF?",
   "En parámetros que reciben URLs: webhooks, importar desde URL, generar PDF o thumbnail, previsualización de links, proxies, y parámetros como url=, callback= o image=. Cualquier función donde vos das una dirección y el server la trae.",
   "procedimiento", ["ssrf", "superficie"], ["¿Dónde aparece el SSRF?", "¿Qué funciones son candidatas a SSRF?"]),
  ("¿Cómo confirmo un SSRF a ciegas?",
   "Usá un canary que controlás (Burp Collaborator, un webhook propio, un dominio tuyo). Si el servidor resuelve y pega a tu dominio, tenés al menos un SSRF ciego. Registrá la IP de origen: suele ser del rango interno del target.",
   "procedimiento", ["ssrf", "ciego", "canary"], ["¿Cómo verifico un SSRF que no devuelve respuesta?", "¿Cómo pruebo SSRF con un colaborador?"]),
  ("¿Qué hace grave a un SSRF en la nube?",
   "Si el server corre en AWS, GCP o Azure, apuntar al endpoint de metadata (169.254.169.254) puede filtrar credenciales temporales. Es de los SSRF de mayor impacto y muchos programas lo pagan alto. Probá lo mínimo para demostrar, no extraigas de más.",
   "hecho", ["ssrf", "cloud", "metadata", "impacto"], ["¿Por qué el SSRF a metadata es tan grave?", "¿Qué pasa con SSRF en AWS?"]),
  ("¿Cómo evado filtros de SSRF?",
   "Si bloquean localhost o 127.0.0.1, probá 127.1, 0.0.0.0, [::1], la IP en decimal u octal, dominios que resuelven a una IP interna, redirecciones (tu URL devuelve un 302 hacia el destino interno) y variantes de mayúsculas. Documentá qué filtro evadiste.",
   "procedimiento", ["ssrf", "evasion", "filtros"], ["¿Cómo salteo la protección de SSRF?", "¿Qué bypass uso si bloquean localhost?"]),
  ("¿Qué cuidado tengo al probar SSRF?",
   "No escanees redes internas enteras ni muevas datos: con demostrar acceso a un recurso interno o a la metadata alcanza. Escanear agresivo puede violar las reglas del programa y tumbar servicios.",
   "advertencia", ["ssrf", "reglas"], ["¿Qué no hago al testear SSRF?", "¿Puedo escanear la red interna con SSRF?"]),
  ("¿Qué diferencia hay entre SSRF ciego y completo?",
   "Ciego: el server hace el request pero no te devuelve la respuesta, lo confirmás por el canary. Completo: te devuelve el contenido de lo que trajo. El completo suele valer más porque permite leer servicios internos.",
   "definicion", ["ssrf", "ciego"], ["¿SSRF ciego vs full?", "¿Qué es un SSRF completo?"]),
 ]),

 ("Inyección (SQL y comandos)",
  "Entrada del usuario que llega a una consulta SQL o al sistema operativo.", [
  ("¿Qué es una inyección SQL?",
   "Cuando entrada del usuario se concatena en una consulta SQL sin sanitizar, podés alterar la consulta: leer otras tablas, saltear el login o, en casos graves, ejecutar comandos en el motor. Es una de las fallas clásicas de mayor impacto.",
   "definicion", ["sqli", "inyeccion"], ["¿Qué es SQLi?", "¿Qué significa inyección SQL?"]),
  ("¿Cómo detecto una inyección SQL?",
   "Meté una comilla simple en parámetros y observá errores SQL o cambios de comportamiento. Probá lógica booleana (' OR '1'='1) y basada en tiempo (' OR SLEEP(5)-- -) si no hay salida visible. Usá sqlmap solo si el programa permite automatización y dentro del scope.",
   "procedimiento", ["sqli", "deteccion"], ["¿Cómo pruebo SQLi?", "¿Cómo sé si un campo es inyectable?"]),
  ("¿Dónde pruebo SQLi además de los campos obvios?",
   "En headers (User-Agent, X-Forwarded-For, Referer), cookies, parámetros JSON anidados, campos de búsqueda, y en filtros u ordenamientos (order by). Cualquier valor que termine en una consulta es candidato.",
   "procedimiento", ["sqli", "superficie"], ["¿Dónde más busco inyección SQL?", "¿Qué entradas no obvias pruebo para SQLi?"]),
  ("¿Qué es una SQLi a ciegas?",
   "No ves el resultado ni los errores. La confirmás por diferencias booleanas (la respuesta cambia según una condición verdadera o falsa) o por tiempo (retardos con SLEEP o WAITFOR). Es más lenta pero igual de válida.",
   "definicion", ["sqli", "ciega"], ["¿Qué es blind SQLi?", "¿Cómo confirmo SQLi sin ver el resultado?"]),
  ("¿Qué es command injection?",
   "Cuando la entrada del usuario llega a una llamada del sistema operativo. Metiendo ;, |, &&, $(...) o backticks podés ejecutar comandos. Buscalo en funciones de ping, conversión de archivos, utilidades de red o export.",
   "definicion", ["command-injection", "rce"], ["¿Qué es inyección de comandos?", "¿Qué es OS command injection?"]),
  ("¿Cómo confirmo command injection a ciegas?",
   "Hacé que el server pegue a un canary tuyo: ; curl http://tu-canary o $(nslookup tu-dominio). Si ves la interacción DNS o HTTP, hay ejecución. No corras comandos destructivos: con una prueba de DNS alcanza para demostrar.",
   "procedimiento", ["command-injection", "canary"], ["¿Cómo verifico ejecución de comandos a ciegas?", "¿Cómo pruebo RCE sin romper nada?"]),
  ("¿Qué falso positivo típico tiene la inyección por tiempo?",
   "Los retardos pueden venir de la carga del servidor, no de tu payload. Repetí varias veces y compará la respuesta con y sin el payload antes de reportar una SQLi o command injection basada en tiempo.",
   "advertencia", ["sqli", "falso-positivo", "tiempo"], ["¿Por qué un SLEEP puede engañarme?", "¿Cómo evito falsos positivos por tiempo?"]),
 ]),

 ("XSS",
  "Ejecutar JavaScript en el navegador de otros usuarios de la aplicación.", [
  ("¿Qué tipos de XSS hay?",
   "Reflejado (el payload viaja en el request y vuelve en la respuesta), Almacenado (queda guardado y se ejecuta para otros usuarios) y DOM (JavaScript del cliente mete entrada en un sink peligroso). El almacenado suele ser el de mayor impacto.",
   "definicion", ["xss", "tipos"], ["¿Cuáles son los tipos de XSS?", "¿Qué diferencia hay entre XSS reflejado y almacenado?"]),
  ("¿Cómo pruebo XSS reflejado?",
   "Meté un marcador único (ej. xss7319) en cada parámetro y buscalo en la respuesta. Donde aparezca, mirá el contexto (HTML, atributo, script, URL) y probá un payload que rompa ese contexto. Confirmá la ejecución con algo visible; si el programa prohíbe alert, usá print() o un callback.",
   "procedimiento", ["xss", "reflejado"], ["¿Cómo testeo XSS reflejado?", "¿Cómo busco un XSS en un parámetro?"]),
  ("¿Cómo encuentro DOM XSS?",
   "Buscá sinks peligrosos en el JavaScript (innerHTML, document.write, eval, location, .html()) y seguí de dónde viene la fuente (location.hash, document.referrer, postMessage). Las DevTools y herramientas como DOM Invader ayudan a rastrearlo.",
   "procedimiento", ["xss", "dom"], ["¿Cómo pruebo DOM XSS?", "¿Qué sinks miro para XSS del lado del cliente?"]),
  ("¿Qué hace valioso a un XSS?",
   "El almacenado que se ejecuta en la sesión de otros usuarios, sobre todo de admin, y permite robar sesión o hacer acciones en su nombre. Un reflejado con mucha interacción o un self-XSS valen poco o nada. Mostrá un impacto concreto.",
   "hecho", ["xss", "impacto"], ["¿Qué XSS se paga bien?", "¿Qué hace que un XSS tenga impacto?"]),
  ("¿Por qué un self-XSS no suele pagarse?",
   "Porque solo afecta a tu propia cuenta. Para que valga, encadenalo (por ejemplo con un CSRF que setee el valor malicioso en la víctima) o demostrá que puede llegar a ejecutarse en otros usuarios.",
   "advertencia", ["xss", "self-xss"], ["¿Sirve un self-XSS?", "¿Por qué me rechazan un self-XSS?"]),
  ("¿Cómo evado filtros de XSS?",
   "Si filtran <script>: probá handlers de eventos (onerror, onload), etiquetas alternativas (<img>, <svg>), mayúsculas mezcladas, codificación y romper el contexto del atributo. Documentá el filtro y cómo lo pasaste.",
   "procedimiento", ["xss", "evasion", "filtros"], ["¿Cómo salteo el filtro de XSS?", "¿Qué pruebo si bloquean la etiqueta script?"]),
  ("¿Por qué importa el contexto de inyección en XSS?",
   "Dónde cae tu entrada define el payload: dentro de <script> necesitás cerrar la cadena o el JS; en un atributo, cerrar las comillas; en HTML, abrir una etiqueta. Identificá el contexto antes de tirar payloads a ciegas.",
   "definicion", ["xss", "contexto"], ["¿Qué es el contexto de inyección?", "¿Por qué el mismo payload no siempre funciona?"]),
 ]),

 ("Autenticación y sesiones",
  "Login, reset de contraseña, 2FA, JWT y account takeover.", [
  ("¿Qué pruebo en el login?",
   "Fuerza bruta respetando el rate-limit (sin pasarte), enumeración de usuarios (mensajes distintos para usuario válido e inválido), fallas en el reset de contraseña, 2FA que se puede saltear, y respuestas que filtran si el usuario existe.",
   "procedimiento", ["auth", "login"], ["¿Qué testeo en la pantalla de login?", "¿Qué fallas busco en el inicio de sesión?"]),
  ("¿Cómo pruebo el flujo de reset de contraseña?",
   "Mirá si el token es predecible, no expira o se puede reusar; si podés cambiar el host o el email en el request para que el link llegue a otra parte; y si al pedir el reset de otra cuenta se filtra el token en la respuesta.",
   "procedimiento", ["auth", "reset", "password"], ["¿Cómo testeo el olvidé mi contraseña?", "¿Qué fallas tiene el reset de password?"]),
  ("¿Qué miro en un JWT?",
   "El header (alg), el payload (claims como user, role, exp) y la firma. Buscá alg:none, firma que no se verifica, secreto HMAC débil que se puede romper por fuerza bruta, o confusión de algoritmo (RS256 a HS256 usando la clave pública como secreto).",
   "procedimiento", ["auth", "jwt"], ["¿Cómo ataco un JWT?", "¿Qué reviso en un token JWT?"]),
  ("¿Cómo pruebo fallas de autorización en la sesión?",
   "Deslogueá y repetí requests: ¿siguen funcionando? Cambiá tu rol en el token o la cookie. Accedé a endpoints de admin con una sesión normal. Verificá si la sesión se invalida al cambiar la contraseña.",
   "procedimiento", ["auth", "sesion"], ["¿Cómo testeo la sesión?", "¿Qué pruebo sobre los tokens de sesión?"]),
  ("¿Cómo busco un bypass de 2FA?",
   "Buscá un endpoint que complete el login sin validar el segundo factor, un código que no expira o no se invalida tras usarse, fuerza bruta del código de 6 dígitos sin límite, o poder saltear el paso cambiando la respuesta o la redirección.",
   "advertencia", ["auth", "2fa", "bypass"], ["¿Cómo salteo el 2FA?", "¿Qué fallas tiene el segundo factor?"]),
  ("¿De dónde suele venir un account takeover?",
   "De un reset de contraseña roto, un IDOR en el cambio de email, un OAuth mal configurado, o un XSS con robo de sesión. Es de lo mejor pagado. Demostralo con dos cuentas tuyas, tomando control de la segunda desde la primera.",
   "hecho", ["auth", "ato", "impacto"], ["¿Cómo logro un account takeover?", "¿Qué lleva a tomar control de una cuenta?"]),
  ("¿Qué reviso en OAuth o SSO?",
   "El redirect_uri (un valor laxo permite robar el code o el token con un open redirect), la ausencia del parámetro state (CSRF de login), y si el token de un cliente sirve en otro. Un redirect_uri mal validado suele terminar en account takeover.",
   "procedimiento", ["auth", "oauth", "sso"], ["¿Qué fallas tiene OAuth?", "¿Cómo ataco el login con Google o SSO?"]),
 ]),

 ("GraphQL y APIs",
  "Superficie amplia donde suele fallar el control de acceso y el rate-limit.", [
  ("¿Cómo empiezo a testear GraphQL?",
   "Probá la introspección (__schema) para sacar todo el esquema: tipos, queries y mutations. Si está deshabilitada, inferí campos por los mensajes de error y wordlists. Herramientas útiles: GraphQL Voyager, InQL y graphw00f.",
   "procedimiento", ["graphql", "introspeccion"], ["¿Por dónde arranco con GraphQL?", "¿Cómo saco el esquema de GraphQL?"]),
  ("¿Por qué GraphQL es terreno fértil para bugs?",
   "Un endpoint expone muchas operaciones. Es común que el control de acceso falle en mutations olvidadas, que haya BOLA en queries por id, y que falte rate-limit porque podés pedir mucho en una sola query.",
   "hecho", ["graphql", "superficie"], ["¿Qué tiene de especial GraphQL para bounty?", "¿Por qué busco bugs en GraphQL?"]),
  ("¿Qué fallas busco en GraphQL?",
   "BOLA o IDOR en queries con id, mutations sin autorización, exposición de campos sensibles, batching de queries para evadir el rate-limit, y DoS por anidamiento profundo. Para el DoS no tumbes el server: con mostrar la query grave alcanza.",
   "procedimiento", ["graphql", "fallas"], ["¿Qué pruebo en un endpoint GraphQL?", "¿Qué vulnerabilidades tiene GraphQL?"]),
  ("¿Cómo encuentro APIs o endpoints ocultos?",
   "Mirá el JavaScript del front para endpoints no documentados, buscá archivos swagger u openapi, probá versiones viejas (/v1/ cuando usan /v2/), y métodos HTTP alternativos (PUT, DELETE, PATCH) donde solo esperan GET.",
   "procedimiento", ["api", "recon", "endpoints"], ["¿Cómo descubro endpoints escondidos?", "¿Dónde encuentro APIs no documentadas?"]),
  ("¿Qué es mass assignment y cómo lo pruebo?",
   "En APIs que aceptan JSON, probá agregar campos que no deberías controlar: \"role\":\"admin\", \"isVerified\":true, \"balance\":999. Si el backend los acepta sin filtrar, es mass assignment y puede dar escalada de privilegios.",
   "advertencia", ["api", "mass-assignment", "escalada"], ["¿Cómo pruebo mass assignment?", "¿Qué pasa si agrego campos extra al JSON?"]),
  ("¿Qué diferencia hay entre BOLA y BFLA?",
   "BOLA: accedés a objetos de otros usuarios cambiando un id. BFLA (Broken Function Level Authorization): accedés a funciones que tu rol no debería, como endpoints de admin. Ambas encabezan el OWASP API Top 10.",
   "definicion", ["api", "bola", "bfla"], ["¿Qué es BFLA?", "¿Diferencia entre BOLA y BFLA?"]),
 ]),

 ("Reporte y triage",
  "Escribir el reporte, estimar severidad y evitar errores que cuestan el pago.", [
  ("¿Cómo escribo un buen reporte?",
   "Título claro con el tipo de bug y el impacto. Resumen de una línea. Pasos para reproducir numerados y exactos (con cuenta de prueba, requests y respuestas). Impacto concreto para el negocio. Sugerencia de arreglo. Capturas o un video corto.",
   "procedimiento", ["reporte", "redaccion"], ["¿Cómo redacto el reporte?", "¿Qué lleva un buen informe de bug?"]),
  ("¿Qué sube la severidad y el pago de un hallazgo?",
   "El impacto real demostrado: datos sensibles, account takeover, RCE o acceso a sistemas internos. Un bug teórico sin impacto paga poco. Encadená fallas menores para mostrar un impacto mayor.",
   "hecho", ["reporte", "severidad", "impacto"], ["¿Qué hace que paguen más?", "¿Cómo aumento la severidad de un reporte?"]),
  ("¿Cómo evito que me marquen el reporte como duplicado?",
   "Si el bug es obvio o superficial, probablemente ya lo reportaron. Buscá fallas más profundas o en funciones nuevas. No podés ver otros reportes, pero los bugs de bajo esfuerzo suelen ser duplicados.",
   "advertencia", ["reporte", "duplicado"], ["¿Cómo evito un duplicado?", "¿Por qué me rechazan por duplicado?"]),
  ("¿Cómo calculo la severidad?",
   "HackerOne usa CVSS. Pensá en el vector (red o local), los privilegios necesarios, la interacción del usuario, y el impacto en confidencialidad, integridad y disponibilidad. Sé honesto: inflar la severidad te baja la reputación.",
   "procedimiento", ["reporte", "cvss", "severidad"], ["¿Cómo estimo el CVSS?", "¿Cómo pongo la severidad de un bug?"]),
  ("¿Qué no debo hacer nunca al testear o reportar?",
   "No accedas a datos de usuarios reales más de lo mínimo para probar, no pivotees a otros sistemas, no extraigas datos masivos, y no divulgues público antes del arreglo. Violarlo puede costarte el pago y la cuenta.",
   "advertencia", ["reporte", "reglas", "etica"], ["¿Qué está prohibido al reportar?", "¿Qué me puede banear de la plataforma?"]),
  ("¿Qué pasa después de enviar un reporte?",
   "El equipo lo triagea: puede pedir más información, marcarlo duplicado o no aplica, o aceptarlo y pagar. Respondé rápido y con datos. Tu reputación y tus señales suben con reportes válidos y bajan con ruido.",
   "hecho", ["reporte", "triage"], ["¿Qué sucede tras mandar el bug?", "¿Cómo es el proceso de triage?"]),
  ("¿Cómo encadeno bugs para más impacto?",
   "Un open redirect más un OAuth laxo da account takeover. Un IDOR de lectura más uno de escritura da control total. Un self-XSS más un CSRF de login lleva el XSS a la víctima. Preguntate siempre: esto, combinado con qué, da impacto real.",
   "procedimiento", ["reporte", "encadenar", "impacto"], ["¿Cómo combino vulnerabilidades?", "¿Cómo aumento el impacto encadenando bugs?"]),
 ]),
]


def slug(name: str) -> str:
    n = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")


def build() -> list[dict]:
    out = []
    for name, desc, cards in MODULOS:
        out.append({
            "format": FORMAT, "name": name, "description": desc, "embedder": "", "cards": [
                {"question": q, "answer": a, "kind": k, "tags": tags, "variants": variants, "source": "semilla"}
                for q, a, k, tags, variants in cards
            ],
        })
    return out


def write(mods: list[dict]) -> list[Path]:
    paths = []
    for m in mods:
        p = HERE / f"{slug(m['name'])}.modulo.json"
        p.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")
        paths.append(p)
    return paths


def main() -> None:
    mods = build()
    paths = write(mods)
    total = sum(len(m["cards"]) for m in mods)
    print(f"{len(mods)} módulos, {total} tarjetas:")
    for m, p in zip(mods, paths):
        print(f"  {len(m['cards']):2d}  {p.name}")

    if "--validar" in sys.argv:
        import os
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="semillas_")
        os.environ["MANAGER_DATA"] = tmp
        os.environ["MANAGER_EMBEDDINGS"] = "hash"
        sys.path.insert(0, str(HERE.parent))
        from manager import store  # noqa: E402
        print("\nvalidando import:")
        for m in mods:
            r = store.import_module(m)
            print(f"  {r['name']}: {r['cards']} tarjetas")
        shutil.rmtree(tmp, ignore_errors=True)

    if "--cargar" in sys.argv:
        import urllib.request
        base = sys.argv[sys.argv.index("--cargar") + 1].rstrip("/")
        print(f"\ncargando en {base}:")
        for m in mods:
            data = json.dumps(m).encode()
            req = urllib.request.Request(base + "/api/modules/import", data=data,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                r = json.loads(resp.read())
            print(f"  {r['name']}: {r['cards']} tarjetas")


if __name__ == "__main__":
    main()
