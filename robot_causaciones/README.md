# Robot de causaciones SICOF

Primera vez en una máquina limpia:

```
uv sync
uv run playwright install chromium
cp .env.example .env
```

Ejecución (con el portal SICOF arrancado):

```
uv run python main.py
```

Lee `datos/por_radicar.xlsx` y escribe `salida/bot.log`, `salida/resultados.xlsx`
y, si una factura agota los reintentos, una captura en `salida/evidencias/`.
