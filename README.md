# Alertas de empleo a Telegram

[![Verificar la regla de pago](https://github.com/talcaldeg/alertas-empleo-telegram/actions/workflows/test.yml/badge.svg)](https://github.com/talcaldeg/alertas-empleo-telegram/actions/workflows/test.yml)

Un bot que revisa tres fuentes de ofertas de trabajo y manda a Telegram solo las que
pasan un filtro explícito. Corre en GitHub Actions, no necesita servidor y usa
únicamente la librería estándar de Python. La frecuencia se configura en el workflow;
acá el `schedule` viene comentado a propósito (ver [Cómo usarlo](#cómo-usarlo)).

El código es la parte fácil. Lo que sigue es el criterio, que es lo que en realidad
decide si esto sirve o si es una fuente más de ruido.

---

## El problema

Las alertas que ofrecen los portales de empleo mandan todo lo que hace match de texto.
Si uno se suscribe a "data analyst en Chile", llegan treinta avisos por semana y en dos
semanas uno los archiva sin leer. La alerta deja de funcionar no porque falle, sino
porque el costo de revisarla superó lo que aporta.

Así que el producto acá no es la integración con las APIs: es **la regla que descarta**.
Todo lo demás existe para alimentarla.

## La regla que decide

Una sola función, `filtro_pago()`, y las tres fuentes están obligadas a llamarla. En
orden:

**1. Media jornada entra siempre, a cualquier sueldo.** Un cargo de 20 horas se evalúa
contra un umbral distinto al de una jornada completa, porque el punto de comparación es
el tiempo que ocupa, no la plata. Poner un piso de sueldo mensual a un part time es
comparar cosas distintas.

**2. Jornada completa con sueldo publicado: manda el techo del rango.** Si la oferta
dice "2.500 - 3.200 USD" y el piso configurado es 3.500, se descarta sin más. Se mira el
techo y no el piso porque el techo es la promesa máxima del aviso; si ni siquiera esa
llega, la negociación no va a llegar tampoco.

**3. Jornada completa sin sueldo publicado: decide el cargo, y si el cargo es ambiguo,
la empresa.** Este es el caso difícil, porque es la mayoría: LinkedIn y casi todos los
ATS de empresa no publican rango.

- Un título senior, lead, manager o superior pasa. La distribución de sueldos de esos
  cargos está lo bastante arriba como para que valga la pena mirar el aviso.
- Un título de nivel *analyst* solo pasa si la empresa está en una lista explícita de
  las que pagan sobre el umbral incluso en ese nivel.

Esa lista es el punto delicado del diseño, así que vale la pena ser preciso sobre qué
significa.

## Las dos listas de empresas

`empresas_pagan_bien` **no** quiere decir "empresa grande", ni "empresa conocida", ni
"empresa donde me gustaría trabajar". Quiere decir exactamente una cosa: *acá un cargo
de nivel analyst supera el umbral*. Es el único criterio que hace pasar un aviso sin
sueldo publicado, así que cualquier otra lectura la contamina.

Y existe una segunda lista, `empresas_no_califican`, que **gana por sobre la primera**.
No es redundante: es la que hace que el error más probable no sea silencioso. Agregar
por entusiasmo una empresa grande a la lista de las que pagan bien no reabre el hueco si
esa empresa ya está en la de exclusión, y el autotest falla si alguien la deja en las
dos a la vez.

Viene vacía en `perfil.example.json`, a propósito. Qué empresa paga cuánto a nivel
analyst depende del mercado de cada uno y del umbral de cada uno, y no corresponde
publicar la tabla de sueldos estimada de nadie. El mecanismo se entrega; la lista la
pone quien lo use.

## Las tres fuentes, y por qué no son intercambiables

| Fuente | Qué entrega | Qué falta |
|---|---|---|
| **GetOnBoard** (API pública) | sueldo, modalidad, seniority, categoría | poco volumen fuera de tecnología |
| **LinkedIn** (endpoint *guest*, sin login) | volumen | ni sueldo ni seniority; solo título, empresa y ubicación |
| **Páginas de empleo de empresas** (Greenhouse, Lever, Ashby, Teamtailor, Eightfold) | ofertas que no se publican en portales | rara vez traen sueldo |

Las tres normalizan a un formato común y entran por la misma función de filtro. Es
deliberado: el modo natural de que este sistema se degrade es que alguien agregue una
fuente nueva y se le olvide aplicarle la regla, y entonces por ahí empieza a entrar todo
lo que las otras dos descartan. Por eso hay una guardia dedicada a eso.

Sobre los ATS vale la pena decir una cosa: la mayoría de las empresas no tiene un backend
propio de empleos, sino un ATS con API pública JSON. Agregar una empresa es averiguar cuál
usa y con qué *slug*, y escribir una línea en el perfil. El lector de cada ATS es genérico,
no hay un scraper por empresa.

## Nota sobre las fuentes

Vale la pena ser explícito sobre qué se consulta y cómo, porque es lo primero que
conviene revisar antes de usar algo así.

**El bot se identifica.** Las tres fuentes mandan el mismo User-Agent, definido una sola
vez en `perfil.py`, que nombra la herramienta y enlaza este repo. No hay suplantación de
navegador. La versión anterior sí simulaba Chrome contra LinkedIn; se comprobó que el
endpoint responde idéntico con un User-Agent honesto (HTTP 200, mismo número de
resultados), así que la simulación no compraba nada y se sacó. Si una fuente dejara de
responder por identificarse de verdad, la respuesta correcta es desactivar esa fuente, no
disfrazarse.

**El volumen es bajo a propósito.** Con la configuración de ejemplo, una corrida son 14
peticiones a LinkedIn espaciadas 1,5 s y una por empresa del registro espaciadas 0,4 s.
Es menos tráfico que una persona revisando los mismos sitios a mano.

**Qué es cada fuente.** Las APIs de Greenhouse, Lever y Ashby son públicas y están
pensadas para consumirse: son las mismas que alimentan las bolsas de empleo de esas
empresas. GetOnBoard publica una API documentada. El caso de LinkedIn es distinto: se usa
el endpoint que el sitio sirve a quien no ha iniciado sesión, así que no hay elusión de
login ni de paywall, pero **sus Términos de Servicio restringen el acceso automatizado**,
con navegador simulado o sin él. Quien use esto debería decidir por sí mismo si activa esa
fuente; se apaga con `fuentes.linkedin.activa: false` en el perfil, y las otras dos siguen
funcionando.

## Los tres errores, y cómo dejaron de poder repetirse

Cada uno de estos pasó de verdad y dejó pasar avisos que debían cortarse. Ninguno se
arregló con un comentario en el código: los tres se convirtieron en una verificación
mecánica que corre en cada push y antes de cada corrida programada.

**1. Meter una empresa en la lista equivocada.** Tener una cadena de retail entre las
que "pagan bien" dejaba pasar justo los cargos de analyst que se querían cortar. Ahora
la lista de exclusión gana por sobre la otra, y el autotest falla si una empresa queda
en ambas. También verifica que la exclusión sirva con la razón social completa alrededor
del nombre, no solo con el nombre pelado.

**2. Detectar media jornada leyendo la descripción.** Los avisos de jornada completa
mencionan "media jornada" o "por horas" de pasada (horarios flexibles, viernes cortos) y
cada mención hacía que el aviso saltara el umbral de sueldo por la rama de part time. La
detección se hace ahora solo sobre el título y el campo de modalidad. El autotest revisa
el **código fuente** para esto: busca cualquier llamada que le pase una descripción a la
función de detección y falla si la encuentra. Es la única forma de cubrir un error que
no se ve desde afuera.

**3. Sacarle el filtro de pago a una fuente.** El autotest pasa una oferta sintética por
cada una de las tres y verifica que las tres la descarten. Si alguien refactoriza una
fuente y se salta el filtro, falla ahí.

Las verificaciones corren sin red y sin enviar nada:

```bash
python alertas_empleos.py --autotest
```

Están hechas para fallar solas. Un invariante que depende de que alguien se acuerde de
revisarlo no es un invariante.

Un detalle de diseño de esas guardias: los montos de prueba son **relativos al umbral
configurado**, no cifras fijas. Si estuvieran escritos a mano, bajar el piso de sueldo a
propósito rompería el autotest, y lo que la guardia tiene que cuidar es la regla ("manda el
techo del rango"), no el número.

## Cómo usarlo

```bash
git clone https://github.com/talcaldeg/alertas-empleo-telegram.git
cd alertas-empleo-telegram

cp perfil.example.json perfil.json     # su umbral, sus empresas, sus keywords
cp config.example.json config.json     # token de @BotFather y su chat_id

python alertas_empleos.py --autotest   # verifica la regla, sin red
python alertas_empleos.py --resumen    # muestra qué enviaría, sin enviar ni marcar visto
python alertas_empleos.py --seed       # marca lo actual como visto, sin enviar (primera vez)
python alertas_empleos.py              # corrida real: envía y marca visto
```

Correr sin flags **envía** a Telegram y marca las ofertas como vistas, así que la corrida
siguiente ya no se las manda. Para probar, use `--resumen`.

Para dejarlo corriendo en la nube: haga fork, cargue `TELEGRAM_BOT_TOKEN` y
`TELEGRAM_CHAT_ID` en *Settings > Secrets and variables > Actions*, y descomente el
`schedule` en `.github/workflows/alertas.yml`. No hace falta servidor ni dejar un PC
prendido.

## El perfil

Todo lo que cambia entre una persona y otra vive en `perfil.json`, fuera del código y
fuera del control de versiones:

| Sección | Qué contiene |
|---|---|
| `pago` | umbral, si el part time pasa libre, las dos listas de empresas, los términos que identifican un cargo de nivel analyst |
| `match` | categorías, palabras de rol, stack, títulos a descartar, seniority bloqueado, ubicaciones aceptadas |
| `fuentes` | qué fuentes están activas, las búsquedas de LinkedIn, el registro de empresas con su ATS |

Si `perfil.json` no existe, el bot corre con `perfil.example.json`, que sí está
versionado. El repo funciona recién clonado, pero con un perfil que no es el de nadie.

## Decisiones de implementación

- **Solo librería estándar.** Sin `requests`, sin `beautifulsoup`. Es un script que corre
  cada dos horas en un runner efímero; cada dependencia es una cosa más que puede
  romperse sola un martes cualquiera.
- **El estado vive en el repo.** `seen_jobs.json` guarda lo ya enviado y el workflow le
  hace commit de vuelta. No hay base de datos porque no hace falta una: el estado son
  unos pocos kilobytes y el historial de git sale gratis.
- **Degrada por fuente.** Si LinkedIn responde 429 o un ATS cambia su endpoint, esa
  fuente se registra en el log y las otras siguen. Una corrida corta se auto-sana en la
  siguiente.
- **Idempotente.** La misma oferta no se manda dos veces, y la corrida se puede repetir
  sin efectos raros.
- **El autotest corre antes de la corrida real**, no solo en CI. Si la regla se rompió,
  la corrida se corta antes de mandar nada al teléfono.

## Qué no hace

No postula, no arma currículos, no puntúa candidatos, no usa un modelo de lenguaje. Lee
tres fuentes, aplica una regla escrita a mano y manda un mensaje. Todo lo que descarta se
puede explicar en una frase, que es justo lo que uno quiere de un filtro que corre solo.

## Licencia

MIT.
