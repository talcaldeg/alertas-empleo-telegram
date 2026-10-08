# Reglas de este repo

Este bot filtra ofertas de empleo y las manda a Telegram. Lo que se escapa del filtro le
llega a alguien al teléfono, así que las reglas de abajo no son estilo: son el producto.
Las cubre `--autotest`, que corre en cada push y antes de cada corrida programada.

## Invariantes (si tocas el filtro, corre el autotest)

1. **El perfil no se hardcodea.** Umbral de sueldo, listas de empresas, palabras de rol,
   búsquedas de LinkedIn y registro de ATS viven en `perfil.json` (o en
   `perfil.example.json` como fallback), nunca en el código. Si necesitas un parámetro
   nuevo, agrégalo al JSON y expórtalo desde `perfil.py`.

2. **Part time tiene su propio piso** (`min_salary_usd_parttime`, más bajo que el de full
   time). Freelance de GetOnBoard y contratos por hora (Himalayas, Arc) se miden con ese
   mismo piso; la tarifa por hora se lleva a mensual con `horas_semana_part_time`
   (`HORAS_MES_PART_TIME` en `perfil.py`). Sin sueldo publicado aplica la misma regla de
   nivel y empresa que el full time. No poner "part-time" en `negative_title`.

3. **El part time se detecta por el título o por el campo `modality`, nunca por la
   descripción.** Los avisos full time mencionan "media jornada" o "por horas" de pasada,
   y un falso positivo se salta el umbral.
4. **`empresas_pagan_bien` significa "acá hasta un cargo analyst supera el umbral".** No
   es "empresa grande" ni "empresa conocida". `empresas_no_califican` gana por sobre la
   otra, y el autotest falla si una empresa queda en las dos listas.

5. **Las cuatro fuentes aplican la misma regla.** `score_job` (GetOnBoard),
   `score_linkedin`, `score_empresa` y `score_contrato` deben llamar a `filtro_pago()`. El autotest
   lo prueba en cada una.

6. **Los casos del autotest son relativos al umbral, no cifras fijas.** Bajar el piso de
   sueldo o de part time, o cambiar las horas por semana, es legítimo y no debe romper la
   guardia: las tarifas de prueba se derivan de ellos. Se cuida la regla, no el número.

7. **El bot se identifica.** Toda petición HTTP usa `perfil.USER_AGENT`; ninguna fuente
   simula un navegador. Una fuente que no responda así se desactiva, no se disfraza.

## Qué no versionar

`config.json` (token de Telegram) y `perfil.json` (la búsqueda de trabajo de alguien)
están en `.gitignore` a propósito. No forzarlos ni pegar sus valores en el README, un
workflow o un commit. En la nube van como Secrets de
GitHub Actions.

## Cómo probar

- `--autotest` (sin red, lo que corre en CI) o `--resumen` (fuentes reales, sin marcar
  visto); el uso completo, en el encabezado de `alertas_empleos.py`.
- Nunca correr sin flags para probar: eso **envía** a Telegram y marca las ofertas como
  vistas, así que la corrida real ya no se las manda.
