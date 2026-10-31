# Plano — Shopee Studio: melhorias pendentes

## Estado e objetivo

Este documento contém apenas o trabalho pendente e o andamento das melhorias em curso. O plano antigo, as etapas executadas e os registros de implantação foram retirados a pedido do usuário. O comportamento implementado está descrito no `README.md`.

**Escopo confirmado:** melhorar a busca de anúncios equivalentes e simplificar a entrega no Telegram para uma única mensagem por produto, com vídeo, descrição e links. O usuário pediu as três entregas num PR só e confirmou os limites propostos do piloto.

**Situação:** as Entregas 1, 2 e 3 estão implementadas na branch `feat/busca-progressiva-video-unico`, com testes automatizados. Faltam a comparação com a API real (execução #29), a conferência do usuário no `pc1` e no Telegram de teste, o merge e o deploy.

## 1. Pendências antes do merge

1. **Comparação real da execução #29.** Rodar `scripts/compare_equivalent_search.py --live` com os cinco produtos de #29 e gravar `scripts/fixtures/run29.json` (páginas sem credenciais). Depois, `--evaluate` com o agy local para medir aprovados, incertos, incompatíveis, rodadas, chamadas e duração. Requer autorização para usar as credenciais da Shopee de produção, só leitura, sem passar pelo chat. Registrar o resultado honestamente, inclusive se nenhum novo link for aprovado.
2. **Revisão visual dos novos aprovados** com o usuário. Se nenhum novo link passar, avaliar o gargalo de evidências (etapa condicionada da seção 3).
3. **Teste do usuário no painel isolado** (`http://192.168.1.250:8792`, dados fictícios de #29, sem worker): conferir os estados dos cards, o diagnóstico da busca, a prévia e os limites em Preferências.
4. **Telegram real em destino de teste autorizado:** o usuário configura o bot e um chat/tópico de teste no painel isolado e toca em **Reenviar vídeo** no card do espelho. Deve chegar uma única mensagem: o vídeo já existente, com nome, descrição copiável e os links numerados. Conferir a cópia da descrição e a abertura de todos os links. Não gasta créditos do Flow.
5. **Merge e deploy** só com autorização explícita. Antes do deploy no TheVault: diagnóstico atual, confirmar que não há vídeos em andamento (jobs antigos com foto continuam como resposta à foto), backup do SQLite, tag de rollback e janela fora do horário das 12:00.

## 2. Critérios de aceite ainda não verificados com dados reais

- Ganho de cobertura da busca nos cinco produtos de #29, sem apresentar candidatos como aprovados.
- Casos 127V × 220V, 480ml × 500ml, kit diferente e formatos distintos continuam sem aprovação indevida no modelo real.
- Duração real por produto dentro do prazo de 10 minutos e uso de quota da API aceitável.
- Mensagem única recebida no cliente Telegram do usuário, com a descrição copiável e todos os links abrindo.

## 3. Detalhes adicionais dos anúncios — etapa condicionada

Se o piloto continuar limitado por falta de voltagem, composição ou variante, investigar uma fonte acessível de detalhes do anúncio. O fluxo atual compara título e imagem; não presumir que especificações e variantes estejam disponíveis na consulta usada. Validar uma fonte real antes de integrar, registrar origem dos atributos e manter incerto quando não houver confirmação. Não contornar login/CAPTCHA nem tratar conteúdo do anúncio como instruções. Essa etapa depende dos resultados do piloto.

## 4. Riscos

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
| E1.5 | Comparador antigo × novo | `scripts/compare_equivalent_search.py`: fixtures por padrão, `--live` com gravação sanitizada, `--evaluate` com agy local; sem `Engine.collect`, banco de produção, Telegram ou Flow | feito; execução real pendente (seção 1) |
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
| E3.6 | Teste real no Telegram | painel isolado com vídeo pronto já existente | pendente (seção 1) |

### Decisões tomadas sem perguntar

- `candidate_budget` continua sendo o tamanho do lote (8; 0 desliga a busca). O total de 24 é um campo novo (`candidate_total`), assim como consultas, páginas, buscas, rodadas e prazo; todos ficam em Preferências → Limites da busca.
- Produto aguardando vídeo fica reservado (excluído de novas consultas). Produto com falha antes do Telegram volta a ser elegível; se outra consulta já o enviou, "Gerar vídeo ↻" é recusado.
- Vídeo recusado pelo Telegram mantém o produto reservado enquanto o arquivo espera o "Reenviar vídeo".
- Pausa da fila, teto atingido e vídeo desligado deixam o produto "não entregue" (com "Gerar vídeo ↻"), não pendente.
- A prévia de uma consulta já enviada mostra a mensagem congelada e o estado de cada produto.
- Fallback do modelo continua entregando o texto automático, agora com o aviso dentro da legenda do vídeo.
