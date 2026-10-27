---
name: release-runbook
description: Roteiro do agente devops para rodar o CI e subir a aplicação da release.
---
# Release

1. `ci_run(deployment_id)` — roda o CI. Falhou? Responda com o passo que quebrou e a causa em uma frase; não suba.
2. A subida só acontece após aprovação humana: `deploy_app(deployment_id)` devolve erro "aguardando aprovação" até lá.
3. Aprovado: `deploy_app(deployment_id)`. Se não ficar saudável, chame `deploy_logs(deployment_id)` e reporte a causa.
4. Informe sempre a URL final (ex.: http://127.0.0.1:8101) e o estado real devolvido pela ferramenta — nunca presuma.
5. A nova release substitui a anterior do mesmo app automaticamente; `deploy_stop` encerra.
