# Reglas de este repo

Este bot filtra ofertas de empleo y las manda a Telegram. Lo que se escapa del filtro le
llega a alguien al teléfono, así que las reglas de abajo no son estilo: son el producto.
Están cubiertas por `python alertas_empleos.py --autotest`, que corre en cada push y
antes de cada corrida programada.

## Invariantes (si tocas el filtro, corre el autotest)

1. **El perfil no se hardcodea.** Umbral de sueldo, listas de empresas, palabras de rol,
   búsquedas de LinkedIn y registro de ATS viven en `perfil.json` (o en
   `perfil.example.json` como fallback), nunca en el código. Si necesitas un parámetro
   nuevo, agrégalo al JSON y expórtalo desde `perfil.py`.

2. **Part time entra a cualquier sueldo.** No poner "part-time" en `negative_title`: la
   media jornada se compara contra el tiempo que ocupa, no contra un piso mensual.

3. **El part time se detecta por el título o por el campo `modality`, nunca por la
   descripción.** Los avisos full time mencionan "media jornada" o "por horas" de pasada,
   y un falso positivo se salta el umbral. El autotest revisa el código fuente para esto.

4. **`empresas_pagan_bien` significa "acá hasta un cargo analyst supera el umbral".** No
   es "empresa grande" ni "empresa conocida". `empresas_no_califican` gana por sobre la
   otra, y el autotest falla si una empresa queda en las dos listas.

5. **Las tres fuentes aplican la misma regla.** `score_job` (GetOnBoard),
   `score_linkedin` y `score_empresa` deben llamar a `filtro_pago()`. El autotest pasa
   una oferta sintética por cada una; si alguien le saca el filtro a una fuente, falla.

6. **Los casos del autotest son relativos al umbral, no cifras fijas.** Bajar el piso de
   sueldo en el perfil es una decisión legítima y no debe romper la guardia. Lo que la
   guardia cuida es la regla, no el número.

## Qué no versionar

`config.json` (token de Telegram) y `perfil.json` (la búsqueda de trabajo de alguien)
están en `.gitignore` a propósito. No forzar su inclusión, ni pegar sus valores en el
README, en un workflow ni en un mensaje de commit. En la nube van como Secrets de
GitHub Actions.

## Cómo probar

- `python alertas_empleos.py --autotest` - sin red, sin enviar nada. Es lo que corre en CI.
- `python alertas_empleos.py --resumen` - consulta las fuentes reales y muestra lo que
  enviaría, sin marcar nada como visto. Ojo: la API de GetOnBoard limita si la consultas
  muchas veces seguidas y la corrida sale corta (se auto-sana en la siguiente).
- Nunca correr sin flags para probar: eso **envía** a Telegram y marca las ofertas como
  vistas, así que la corrida real ya no se las manda.
