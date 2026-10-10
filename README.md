# Shopee Studio

Painel pessoal para pesquisar, selecionar e acompanhar ofertas de afiliados Shopee. Roda em um container no Unraid, com SQLite persistente e interface em português.

## O que faz

- Consulta a API oficial, trata respostas sem campo errors e repete falhas transitórias até três vezes.
- Filtra nicho, categoria, preço, avaliação, vendas e comissão; explica a classificação.
- Usa comissão estimada de cliente existente quando disponível, sem misturar taxas de novos clientes. Normalização logarítmica reduz a influência de valores extremos.
- Respeita os limites por loja/categoria; prefere mostrar menos ofertas a flexibilizar filtros silenciosamente.
- Evita repetir produtos enviados por um intervalo configurável; produtos descartados ou já divulgados ficam excluídos até mudar sua triagem.
- Histórico de consultas, preços observados, decisões e entregas; SQLite persiste nas atualizações.
- Consulta manual apenas prepara a prévia. Envio manual exige confirmar no painel.
- Configuração do Telegram pela interface: token, chat/grupo e tópico (message_thread_id). Token vazio preserva o já salvo; tópico vazio remove o destino de tópico.
- Agenda diariamente em America/Sao_Paulo, recupera o horário perdido do dia ao reiniciar e tenta falhas novamente após 5 e 10 minutos.
- Entrega incerta não é repetida automaticamente: confira o Telegram. Não há garantia de exactly-once na API do Telegram.
- Login no painel, proteção CSRF e limitação de tentativas de login. Segredos ficam fora do Git e da imagem.

## Unraid / Compose Manager

1. Crie uma stack chamada shopee-studio com o conteúdo de compose.yaml deste repositório.
2. Coloque um .env junto ao Compose, baseado em .env.example. Defina PANEL_PASSWORD. As credenciais da Shopee e do Telegram podem ser preenchidas depois pela interface.
3. Execute Compose Up. Acesse http://IP-DO-UNRAID:8787 e use a senha PANEL_PASSWORD.
4. Em Preferências, confira filtros, configure Telegram e o tópico. Faça uma consulta e revise a prévia.
5. Habilite envio automático quando o destino e o ranking estiverem corretos. Por padrão, só gera prévias.

A porta 8787 deve estar livre. PANEL_PORT altera a porta externa. Use BIND_IP para restringir à interface LAN desejada. Com proxy HTTPS, use COOKIE_SECURE=true e acesse apenas o endereço HTTPS. Não exponha diretamente a porta na internet.

O volume shopee-studio_shopee-data guarda banco e chave de sessão. Faça backup desse volume com o container parado. Ele também contém os segredos da Shopee e do Telegram configurados na interface; trate o backup como confidencial. Não use docker compose down -v ao atualizar.

## Atualizações GitHub → Unraid

Cada push na branch main executa os testes e publica:
- ghcr.io/saladfruiter/shopee-studio:latest
- ghcr.io/saladfruiter/shopee-studio:sha-COMMIT_COMPLETO

A stack de produção usa image:, sem build local. O Compose Manager pode comparar a imagem instalada com a imagem publicada e exibir atualização disponível ao usar Check for Updates. Habilite a verificação periódica nas configurações do Compose Manager para receber a indicação automaticamente. Uma alteração só fica disponível quando o workflow termina com sucesso.

O primeiro pacote GHCR precisa ter visibilidade Public em GitHub → Packages → shopee-studio → Package settings. Um repositório público sozinho não torna o pacote público. Depois disso, o servidor não precisa de login para baixar.

A atualização é manual: use Update Stack / Compose Up com pull, ou:
    docker compose pull
    docker compose up -d

O serviço não se atualiza sozinho. A interface exibe o SHA da versão instalada. Para reverter, use uma tag sha- anterior em image: e recrie o container; preserve o volume.

## Desenvolvimento e testes

Python 3.13:
    python -m venv .venv
    # Ative o ambiente virtual
    pip install -r requirements.txt
    python -m unittest discover -s tests -v
    # Copie .env.example para .env e preencha
    python app.py

Servidor WSGI Waitress, porta 8787. Com Docker local:
    docker compose -f compose.dev.yaml up --build

Os testes usam respostas simuladas e não enviam mensagens. Não existe senha padrão. Não publique .env, data/, arquivos zip antigos ou logs.

## Limites e operação

- A classificação é uma heurística configurável, não previsão de vendas ou comissão garantida.
- Concorrência de vídeos e quantidade de avaliações não são coletadas; valide manualmente.
- Se a busca exceder o limite de páginas, a execução falha explicitamente em vez de enviar ranking parcial.
- Histórico registra ofertas elegíveis observadas; não é histórico completo do catálogo.
- Logs recentes ficam limitados a 1.000 eventos. Consultas/ofertas são mantidas no SQLite para preservar histórico.
- Um único processo/container deve usar o banco. Não escale réplicas.
- Desative eventual tarefa antiga ShopeeAffiliateTop5Daily no Windows ao migrar para o Unraid, para evitar dois agendamentos.

## Scripts legados

Na cópia local original, daily_shopee_top5.ps1, docker-schedule.sh e install_daily_task.ps1 foram preservados; eles não fazem parte desta publicação nem são usados pelo novo container. O fluxo recomendado é o painel. Não execute os dois agendadores ao mesmo tempo.
