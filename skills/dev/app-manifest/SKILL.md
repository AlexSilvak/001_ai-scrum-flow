---
name: app-manifest
description: Como entregar uma aplicação executável para o CI/CD local subir (arquivo flow-app.toml).
---
# flow-app.toml

Se a história entrega algo que roda (site, API), grave os arquivos da aplicação e um `flow-app.toml` na pasta da história
(ex.: `US-001/flow-app.toml`, `US-001/app.py`). O agente devops usa este arquivo para rodar o CI e subir o app.

```toml
name = "login"
workdir = "."                       # relativo à pasta do manifesto
[ci]
steps = [["python", "-m", "pytest", "-q"]]   # opcional; sem isto só compila os .py
[run]
command = ["python", "app.py"]
port    = 8101                      # obrigatório, entre 8100 e 8199
health  = "/"                       # opcional
```

Regras: o app deve ler a porta da variável de ambiente `PORT` e escutar em `127.0.0.1`; dependências em `requirements.txt`
(se existir, o CI cria um venv e instala); comandos sem shell — só python, pip, pytest, uv, node, npm, flask, gunicorn, uvicorn.
Não escreva segredos nos arquivos.
