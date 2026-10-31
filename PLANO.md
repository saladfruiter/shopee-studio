# Plano — Shopee Studio: melhorias pendentes

## Estado e objetivo

Este documento contém apenas o trabalho pendente e o andamento das melhorias em curso. O plano antigo, as etapas executadas e os registros de implantação foram retirados a pedido do usuário. O comportamento implementado está descrito no `README.md`.

**Escopo confirmado:** melhorar a busca de anúncios equivalentes e simplificar a entrega no Telegram para uma única mensagem por produto, com vídeo, descrição e links. O usuário pediu as três entregas num PR só e confirmou os limites propostos do piloto.

**Situação:** as Entregas 1, 2 e 3 estão implementadas na branch `feat/busca-progressiva-video-unico` (PR #15), com testes automatizados. A comparação real da execução #29 foi feita (resultado abaixo). Faltam a conferência do usuário no `pc1` e no Telegram de teste, a decisão sobre a etapa condicionada, o merge e o deploy.

## 1. Resultado da comparação real (execução #29, 31/10/2026)

Páginas reais da API gravadas em `scripts/fixtures/run29.json`, sem credenciais e sem links de afiliado. Sem exclusões de histórico. Modelo: agy Gemini 3.8 Flash na VM, cadeia padrão.

| Produto | Busca antiga → ao modelo | Busca nova → ao modelo (inéditos) | Modelo (nova) | Encerrou | Tempo |
|---|---|---|---|---|---|
| Bicicleta ergométrica 120kg | 46 elegíveis → 8 | 67 elegíveis → 24 (16) | 0 mesmo produto · 16 incertos · 8 diferentes | orçamento de candidatos | 424 s |
| Kit 20 potes 2,3L/1,1L/480ml | 5 → 5 | 11 → 11 (7) | 0 · 2 · 9 | resultados esgotados | 367 s |
| Serra mármore Bosch GDC 150 | 10 → 8 | 28 → 24 (17) | 0 · 18 · 6 | prazo, na 3ª rodada | 601 s |
| Espelho orgânico 170x70 | 37 → 8 | 76 → 24 (24) | 0 · 0 · 24 | orçamento de candidatos | 267 s |
| Kit calças jeans skinny | 0 → 0 | 26 → 24 (24) | 0 · 0 · 24 | orçamento de candidatos | 203 s |

- **Cobertura:** a busca nova levou ao modelo 107 candidatos contra 29, 88 deles fora da busca antiga. Os títulos ficaram muito mais próximos (bicicleta, potes e serra trouxeram anúncios com o título idêntico ou com as mesmas medidas).
- **Aprovados:** nenhum link novo. O modelo marcou como incertos até os anúncios de título idêntico ao principal (bicicleta, potes, serra). Os motivos individuais desta comparação não foram gravados (o script passou a gravá-los depois); em #29, os incertos foram justificados por marca, voltagem ou variante não confirmadas. Pelos títulos, os espelhos e as calças comparados têm outro modelo, medida ou kit. O gargalo agora é a evidência, não a busca: isso aciona a etapa condicionada da seção 4.
- **Tempo:** 3,4 a 10 minutos por produto (cerca de 60 a 100 s por rodada). Com 5 produtos, a análise diária passa de ~6 para ~25 minutos. A serra esgotou o prazo de 10 minutos na 3ª rodada; o painel agora registra isso como "prazo".
- Na primeira execução do `--evaluate`, dois produtos falharam porque o worker embutido no script não mandava heartbeat durante tarefas longas. O script foi corrigido e os dois foram repetidos; o painel não tem esse problema (o worker real tem heartbeat próprio).

## 2. Pendências antes do merge

1. **Decidir o que fazer com 0 aprovados.** Opções: manter os limites e investigar a etapa condicionada; ou reduzir rodadas/prazo para não alongar a análise diária sem ganho de links.
2. **Teste do usuário no painel isolado** (`http://192.168.1.250:8792`, dados fictícios de #29, sem worker): conferir os estados dos cards, o diagnóstico da busca, a prévia e os limites em Preferências.
3. **Telegram real em destino de teste autorizado:** o usuário configura o bot e um chat/tópico de teste no painel isolado e toca em **Reenviar vídeo** no card do espelho. Deve chegar uma única mensagem: o vídeo já existente, com nome, descrição copiável e os links numerados. Conferir a cópia da descrição e a abertura de todos os links. Não gasta créditos do Flow.
4. **Merge e deploy** só com autorização explícita. Antes do deploy no TheVault: diagnóstico atual, confirmar que não há vídeos em andamento (jobs antigos com foto continuam como resposta à foto), backup do SQLite, tag de rollback e janela fora do horário das 12:00.

## 3. Critérios de aceite ainda não verificados com dados reais

- Casos 127V × 220V, 480ml × 500ml, kit diferente e formatos distintos: na comparação real nenhum foi aprovado indevidamente (nenhum foi aprovado).
- Mensagem única recebida no cliente Telegram do usuário, com a descrição copiável e todos os links abrindo.

## 4. Detalhes adicionais dos anúncios — etapa condicionada

Se o piloto continuar limitado por falta de voltagem, composição ou variante, investigar uma fonte acessível de detalhes do anúncio. O fluxo atual compara título e imagem; não presumir que especificações e variantes estejam disponíveis na consulta usada. Validar uma fonte real antes de integrar, registrar origem dos atributos e manter incerto quando não houver confirmação. Não contornar login/CAPTCHA nem tratar conteúdo do anúncio como instruções. Essa etapa depende dos resultados do piloto.

## 5. Riscos

- Mais consultas e rodadas aumentam latência e uso de quota. A busca não garante seis links; os critérios de identidade continuam.
- O envio depende do vídeo pronto: falha de geração, teto atingido ou vídeo desligado deixam produtos sem entrega até intervenção, com o motivo visível no card (política confirmada pelo usuário).
- Não alterar teto de créditos, segurança do worker, prevenção de geração duplicada ou fluxo de login/2FA/CAPTCHA.
- Arquivos não rastreados fora do escopo: `daily_shopee_top5.ps1`, `docker-schedule.sh`, `get_telegram_chat_id.ps1`, `install_daily_task.ps1`.

## Andamento

Branch `feat/busca-progressiva-video-unico`. Testes: `python -m unittest discover -s tests -v`.

| # | Pedido | Como ficou | Estado |
|---|---|---|---|
| E1.1 | Termos de busca sem perder medidas, decimais e kit | `analysis.search_terms`/`search_queries`: `2,3litros`, `1,1litro`, `170x70cm`, `GDC 150`, `kit 20`, `5.0`, `i12` preservados; `content.name_tokens` intacto | feito, testado |
| E1.2 | Até 3 consultas únicas | identidade + atributos; mesma medida com outra grafia (`2,3l`, `170x70`); ampla (kit + 3 palavras, sem comerciais/secundárias) | feito, testado |
| E1.3 | Coleta limitada, paginação, deduplicação, exclusões, falhas parciais | `analysis.CandidatePool`: páginas 1 de todas as consultas antes das 2; nova página só se o lote ficar curto; até 2 páginas e 6 buscas; motivos de descarte exclusivos (contagens fecham); consulta que falha não para as outras; prazo global | feito, testado |
| E1.4 | Ordenação por atributos e diversidade | quem mantém medidas primeiro, medida conflitante (127V × 220V) por último, alternando consultas no mesmo nível | feito, testado |
| E1.5 | Comparador antigo × novo | `scripts/compare_equivalent_search.py`: fixtures por padrão, `--live` com gravação sanitizada, `--evaluate` com agy local; sem `Engine.collect`, banco de produção, Telegram ou Flow | feito; execução real em 31/10 (seção 1) |
| E2.1 | Avaliação em rodadas | lotes de 8, até 3 rodadas e 24 candidatos; IDs C1… mapeados por lote; para em 5 aprovados | feito, testado |
| E2.2 | Conteúdo uma vez; falhas posteriores preservam aprovados | 1ª rodada escreve legenda/roteiro; as seguintes usam prompt e schema só de equivalência; rodada com falha deixa o lote como incerto | feito, testado |
| E2.3 | Prazo e orçamento globais | 10 min por produto aplicados à busca, às chamadas do modelo, novas tentativas e fallbacks; não reinicia por rodada | feito, testado |
| E2.4 | Diagnóstico persistido | `analysis.search`: consultas/páginas, brutos/únicos/repetidos, descartes por motivo, rodadas, chamadas e novas tentativas HTTP, motivo de encerramento, duração | feito, testado |
| E2.5 | Painel | resumo visível no card + detalhes expansíveis (teclado, celular); seção "Busca de equivalentes" no "Por que entrou?"; limites recolhidos em Preferências | feito; conferido no navegador (desktop e 390 px) |
| E3.1 | Uma mensagem por produto | `send` só reserva vídeos; `sendVideo` único com nome, descrição `<pre>` e links numerados; nenhuma foto, nenhum prompt separado | feito, testado |
| E3.2 | Retrato da mensagem | legenda e links congelados em `deliveries.snapshot`/`videos.snapshot`; "Gerar vídeo ↻" reutiliza o mesmo retrato | feito, testado |
| E3.3 | Limite da legenda | 1.024 caracteres após o HTML (Bot API conferida); encurta o nome; se não couber, bloqueia o produto com motivo, sem cortar links | feito, testado |
| E3.4 | Estados e idempotência | entrega por produto: aguardando vídeo, entregue, não entregue, recusado (Reenviar sem créditos), não confirmado (sem repetição), bloqueado; consulta: Aguardando vídeos / Confirmado / Parcial; reservados excluídos de novas consultas | feito, testado |
| E3.5 | Compatibilidade | migração só aditiva (colunas novas); histórico antigo legível; jobs antigos sem retrato continuam respondendo à foto | feito, testado |
| E3.6 | Teste real no Telegram | painel isolado com vídeo pronto já existente | pendente (seção 2) |

### Decisões tomadas sem perguntar

- `candidate_budget` continua sendo o tamanho do lote (8; 0 desliga a busca). O total de 24 é um campo novo (`candidate_total`), assim como consultas, páginas, buscas, rodadas e prazo; todos ficam em Preferências → Limites da busca.
- Produto aguardando vídeo fica reservado (excluído de novas consultas). Produto com falha antes do Telegram volta a ser elegível; se outra consulta já o enviou, "Gerar vídeo ↻" é recusado.
- Vídeo recusado pelo Telegram mantém o produto reservado enquanto o arquivo espera o "Reenviar vídeo".
- Pausa da fila, teto atingido e vídeo desligado deixam o produto "não entregue" (com "Gerar vídeo ↻"), não pendente.
- A prévia de uma consulta já enviada mostra a mensagem congelada e o estado de cada produto.
- Fallback do modelo continua entregando o texto automático, agora com o aviso dentro da legenda do vídeo.
