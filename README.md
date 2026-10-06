# Azure DevOps Exporter

Exportador de Azure DevOps Services orientado a Azure Boards y gestión de
proyectos. Descarga metadatos de proyecto, equipos, áreas, iteraciones, tipos
de work item, campos, work items, revisiones, actualizaciones, comentarios,
consultas, dashboards y wikis.

## Seguridad

El PAT nunca se guarda en `config.yaml`. Debe estar en esta variable de entorno:

```powershell
$env:AZURE_DEVOPS_EXT_PAT = "TU_PAT"
```

## Instalación en Windows 11

Abre PowerShell en esta carpeta y ejecuta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Si PowerShell impide activar el entorno:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Configuración

En el fichero config.yaml se han de configurar tanto la "organization_url" como el "project", además de la versión de la API, tamaños de página, reintentos en caso de error, componentes/adjuntos que se exportarán o no, directorio de salida, ...

## Comprobación de acceso

```powershell
python export.py --test
```

## Exportación completa

```powershell
python export.py
```

## Resultado

Los datos se guardan en:

- `output/raw/*.jsonl`: respuesta detallada para conservar trazabilidad.
- `output/database/azuredevops.db`: SQLite para consultas y análisis.
- `output/manifest.json`: recuentos, fechas, avisos y errores.
- `output/logs/export.log`: registro de ejecución.

## Consultas rápidas en SQLite

Ejemplo de número de work items por estado:

```sql
SELECT
    json_extract(fields_json, '$."System.State"') AS estado,
    COUNT(*) AS cantidad
FROM work_items
GROUP BY estado
ORDER BY cantidad DESC;
```

Ejemplo de tareas asignadas por persona:

```sql
SELECT
    json_extract(fields_json, '$."System.AssignedTo".displayName') AS responsable,
    COUNT(*) AS cantidad
FROM work_items
GROUP BY responsable
ORDER BY cantidad DESC;
```

## Notas

- El programa no descarga binarios ni contenido de repositorios.
- Los adjuntos aparecen dentro de las relaciones de los work items.
- Una respuesta `403` suele indicar que faltan permisos del usuario o del PAT.
- Una respuesta `401` suele indicar que el PAT es incorrecto, ha expirado o no
  está disponible en `AZURE_DEVOPS_EXT_PAT`.
- WIQL puede estar sujeto a límites del servicio. El programa genera un aviso
  si recibe exactamente 20.000 resultados.
