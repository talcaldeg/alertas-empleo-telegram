# Reglas de este repo

Este bot filtra ofertas de empleo y las manda a Telegram. Lo que se escapa del filtro le
llega a alguien al teléfono: las reglas de abajo son el producto. Las cubre `--autotest`,
que corre en cada push y antes de cada corrida. El porqué de cada una está en el README.

## Invariantes (si tocas el filtro, corre el autotest)

1. **El perfil no se hardcodea.** Sueldo, empresas, palabras de rol, ubicación, bonos de
   puntaje, búsquedas y registro de ATS viven en `perfil.json` (fallback:
   `perfil.example.json`) y se exportan desde `perfil.py`. Un país, ciudad, rubro o
   herramienta escrito en un `.py` es un defecto, también en el autotest.
   `match.ubicacion` no tiene valor por defecto.

2. **LinkedIn mide la ubicación con `es_pais()`, no con `es_local()`**: la ciudad sola
   deja pasar homónimas de otros países. Un rótulo local sin país se agrega entero a
   `pais_terms`; no se afloja la regla.

3. **Part time tiene su propio piso** (`min_salary_usd_parttime`). Freelance y contratos
   por hora usan ese piso; la tarifa por hora se lleva a mensual con
   `horas_semana_part_time`. No poner "part-time" en `negative_title`.

4. **El part time se detecta por el título o por `modality`, nunca por la descripción.**

5. **`empresas_pagan_bien` significa "acá hasta un cargo analyst supera el umbral"**, no
   "empresa grande". `empresas_no_califican` gana, y ninguna empresa va en las dos.

6. **Las cuatro fuentes llaman a `filtro_pago()`**: `score_job`, `score_linkedin`,
   `score_empresa` y `score_contrato`.

7. **Los casos del autotest salen del perfil cargado**: montos relativos al umbral;
   títulos (`_titulos_de_prueba()`), empresas y lugares, de sus listas. Lo que un perfil
   no permite armar se omite y se cuenta. Al tocarlo, correrlo también con un
   `perfil.json` que no sea de datos, en una copia fuera del repo.

8. **El bot se identifica.** Toda petición usa `perfil.USER_AGENT`; una fuente que no
   responda así se desactiva, no se disfraza.

## Qué no versionar

`config.json` (token de Telegram) y `perfil.json` (la búsqueda de alguien) están en
`.gitignore` a propósito. No forzarlos ni pegar sus valores en el README, un workflow o un
commit. En la nube van como Secrets de GitHub Actions.

## Cómo probar

- `--autotest` (sin red) o `--resumen` (fuentes reales, sin marcar visto).
- Nunca correr sin flags para probar: eso **envía** a Telegram y marca las ofertas como
  vistas.
