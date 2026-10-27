---
name: code-review-checklist
description: Checklist de verificação de entregas contra critérios de aceite.
---
# Verificação

1. Veja `arquivos_entregues` na tarefa e leia CADA arquivo com `read_workspace_file` usando o caminho completo (ex.: `US-001/index.html`). Nunca passe só a pasta.
2. Para cada critério de aceite, procure a evidência no CÓDIGO/arquivo (não apenas no `entrega.md`) e marque como evidenciado ou não.
3. Se a tarefa trouxer `pytest`, ele já rodou no sandbox. Para rodar de novo use `qa_run_tests` (item_id). Você NÃO consegue aprovar sem pytest verde nesta rodada.
4. Chame `work_item_verdict` UMA vez: `passed=true` somente se todos estiverem evidenciados; senão `passed=false` listando exatamente quais critérios faltam e em que arquivo corrigir.
5. Conflito de interesses: não verifique itens que você desenvolveu.
