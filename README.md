# zpl2pdf

Converte arquivos ZPL (Zebra Programming Language) em PDFs prontos para
impressão, numa grade calculada automaticamente para comportar exatamente
a quantidade de etiquetas pedida — a etiqueta renderizada é sempre
redimensionada para caber em cada célula (nunca a geração é bloqueada por
"não caber").

O tipo de arquivo é detectado automaticamente pelo número de blocos
`^XA...^XZ` encontrados:

1. **Uma única etiqueta** — repetida `labels_per_page` vezes por página, ao
   longo de `pages` páginas idênticas.
2. **Várias etiquetas distintas** (ex: produtos diferentes) — você escolhe
   entre dois modos:
   - **Caber tudo em 1 página**: grade dimensionada para o total de
     etiquetas do arquivo.
   - **Distribuir em várias páginas**: você informa só `labels_per_page`;
     o número de páginas é calculado automaticamente
     (`ceil(N / labels_per_page)`) e a última página, mesmo parcial, usa o
     mesmo tamanho de célula das páginas cheias.

## Instalação

```powershell
cd zpl2pdf
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` traz só o que a aplicação usa em runtime (CLI + API —
reportlab, requests, PyYAML, Pillow, Flask); é o mesmo arquivo que a função
serverless do Vercel instala, então é mantido enxuto de propósito. Para
rodar a UI Streamlit localmente ou a suíte de testes, instale também
`requirements-dev.txt`:

```powershell
pip install -r requirements.txt -r requirements-dev.txt
```

(Opcional) instalar como pacote com o comando `zpl2pdf` disponível no PATH:

```powershell
pip install -e .
```

> A renderização da etiqueta usa a API pública do Labelary
> (http://api.labelary.com) — é necessária conexão com a internet. Não há
> instalação local de impressora/driver ZPL.
>
> **Limite de requisições (plano gratuito):** o plano gratuito do Labelary
> aceita no máximo **3 requisições/segundo**. Para respeitar esse limite com
> margem de segurança, `LabelaryRenderer` espaça as chamadas a ~2/s e, se
> ainda assim receber HTTP 429, tenta novamente automaticamente com backoff
> exponencial (1s, 2s, 4s, ... até 5 tentativas) antes de desistir. Na
> prática, isso significa que **arquivos com muitas etiquetas distintas (não
> cacheadas ainda) demoram mais para renderizar** — a interface web mostra
> uma barra de progresso para deixar isso claro. Se uma etiqueta continuar
> falhando após todas as tentativas, o processo para com uma mensagem
> indicando qual etiqueta falhou, em vez de gerar um PDF incompleto; espere
> um pouco e tente novamente. Para uso intenso (muitas etiquetas, muitos
> arquivos), o Labelary também é distribuído como imagem Docker para
> self-hosting (veja a documentação oficial do Labelary) — rodando sua
> própria instância local, esse limite de requisições/segundo do plano
> gratuito deixa de se aplicar.

## Uso básico

### Cenário 1 — etiqueta única repetida

```powershell
python -m zpl2pdf.cli `
  --input examples\label_30x20mm.zpl `
  --output saida.pdf `
  --sheet-size A4 `
  --labels-per-page 24 --pages 3 `
  --margin-top 10 --margin-bottom 10 --margin-left 10 --margin-right 10 `
  --gap-x 2 --gap-y 2 `
  --dpi 203
```

Isso gera um PDF de 3 páginas idênticas, cada uma com a etiqueta de
`examples\label_30x20mm.zpl` (30x20mm) repetida 24 vezes numa grade 4x6
calculada automaticamente para a A4 com as margens/gap informados — a
etiqueta é redimensionada (mantendo a proporção) para caber em cada célula
— 72 etiquetas no total.

Se o pacote foi instalado com `pip install -e .`, o mesmo comando pode ser
chamado como `zpl2pdf --input ... --output ...`.

### Cenário 2 — várias etiquetas distintas, cabendo tudo em 1 página

```powershell
python -m zpl2pdf.cli `
  --input examples\label_multiplas.zpl `
  --output saida.pdf `
  --multi-mode fit-one-page
```

`examples\label_multiplas.zpl` tem 7 etiquetas distintas (pedidos #1001 a
#1007); a grade é dimensionada para exatamente 7 células numa única
página, cada etiqueta redimensionada para a célula correspondente.

### Cenário 3 — várias etiquetas distintas, paginadas

```powershell
python -m zpl2pdf.cli `
  --input examples\label_multiplas.zpl `
  --output saida.pdf `
  --multi-mode paginate --labels-per-page 3
```

Com 7 etiquetas e `--labels-per-page 3`, a grade é dimensionada para 3
células (1 col x 3 lin) e o número de páginas é calculado automaticamente:
`ceil(7 / 3) = 3` páginas (as duas primeiras com 3 etiquetas cada, a
última com apenas 1 — usando o mesmo tamanho de célula das anteriores, com
as posições sobrando em branco).

Um arquivo com mais de uma etiqueta **exige** `--multi-mode`; sem ele, a
CLI para com uma mensagem explicando as duas opções.

### Usando um arquivo de configuração

Em vez de repetir todos os parâmetros na linha de comando, use `--config`
apontando para um YAML ou JSON (veja [`config.example.yaml`](config.example.yaml)):

```powershell
python -m zpl2pdf.cli --input examples\label_30x20mm.zpl --output saida.pdf --config config.example.yaml
```

Qualquer argumento passado explicitamente na CLI tem prioridade sobre o
valor do arquivo de config, que por sua vez tem prioridade sobre os
defaults internos (A4, retrato, 1 etiqueta/página, 1 página, margens de
10mm, gap de 2mm, 203dpi).

### Etiquetas sem `^PW`/`^LL` (fallback manual)

Quando o ZPL não define a largura/altura da etiqueta, informe manualmente:

```powershell
python -m zpl2pdf.cli --input examples\label_texto.zpl --output saida.pdf `
  --label-width 63.5 --label-height 38.1 --labels-per-page 6
```

Se a etiqueta não define `^PW`/`^LL` e nenhum fallback for informado, o
programa para com um erro claro em vez de adivinhar um tamanho.

### A quantidade pedida é sempre respeitada (redimensionamento automático)

Você não escolhe colunas/linhas diretamente — informa quantas etiquetas
quer por página (`--labels-per-page`) e o sistema calcula uma grade (cols x
rows) com **exatamente** essa quantidade de células, dado o tamanho da
folha, as margens e o gap. A etiqueta (renderizada uma única vez, no
tamanho original) é então **redimensionada** para caber em cada célula:

- Por padrão, a proporção original é preservada e a etiqueta fica
  centralizada na célula (não distorce o código de barras).
- Com `--stretch`, a proporção é ignorada e a etiqueta é esticada para
  ocupar 100% da célula (preenche melhor o espaço, mas pode distorcer).

Isso significa que a quantidade pedida **nunca é rejeitada** por "não
caber fisicamente" — pedir 800 etiquetas numa A4 gera 800 células bem
pequenas, com a etiqueta bastante reduzida, em vez de um erro. A escala
final é logada:

```
Célula: 5.7x6.7mm · modo proporcional · escala: 19%.
A etiqueta foi reduzida para 19% do tamanho original — o código de barras
pode ficar difícil de ler fisicamente nessa escala.
```

Esse aviso (abaixo de 50% do tamanho original, ver `LOW_SCALE_WARNING_THRESHOLD`
em [`zpl2pdf/layout.py`](zpl2pdf/layout.py)) é apenas informativo — não
impede a geração do PDF.

Ao escolher a grade, `calculate_grid` primeiro restringe as opções aos
pares de divisores de `labels_per_page` mais próximos de um quadrado
(evitando grades degeneradas como 1 coluna x N linhas quando existe uma
divisão mais equilibrada) e, entre as orientações empatadas nesse critério
(ex: 4x6 vs. 6x4), escolhe a que resulta na maior escala possível para a
etiqueta — nesse desempate, a proporção da etiqueta original decide a
orientação da grade.

## Interface web (Streamlit)

Além da CLI, há uma interface web simples em [`app.py`](app.py), que reutiliza
os mesmos módulos (`parser`, `renderer`, `layout`, `pdf_builder`) — nenhuma
lógica é duplicada, só a orquestração e a apresentação mudam.

```powershell
pip install -r requirements.txt -r requirements-dev.txt
streamlit run app.py
```

Isso abre a interface em `http://localhost:8501`. Fluxo de uso:

1. Faça upload de um arquivo `.zpl`. O número de etiquetas distintas
   detectadas é exibido imediatamente.
2. Ajuste os parâmetros na sidebar (tamanho de folha, orientação, margens,
   gaps, dpi, o tamanho de fallback da etiqueta e o checkbox **"Esticar
   para preencher célula"**).
3. Dependendo do arquivo:
   - **Uma etiqueta**: informe quantas cópias por página e quantas
     páginas.
   - **Várias etiquetas distintas**: escolha entre **"Caber tudo em 1
     página"** (sem campos extras — a grade é dimensionada para o total de
     etiquetas) e **"Distribuir em várias páginas"** (informe só
     "Etiquetas por página"; o campo de páginas some, substituído por uma
     mensagem como "Serão geradas 4 páginas para cobrir as 20 etiquetas").
4. A grade calculada e a escala resultante são exibidas assim que os
   parâmetros são válidos — a quantidade pedida é sempre respeitada,
   redimensionando a(s) etiqueta(s) para caber. Se a escala calculada
   ficar abaixo de 50% do tamanho original (a pior entre todas as
   etiquetas do arquivo, quando há mais de uma), um aviso não bloqueante
   alerta que o código de barras pode ficar difícil de ler fisicamente.
5. Clique em **"🔍 Prévia da 1ª página"** para conferir a grade montada
   antes de gerar o PDF completo — no modo "paginar", a prévia renderiza
   só as etiquetas da 1ª página, sem gastar chamadas de API nas restantes.
6. Clique em **"📄 Gerar PDF"** para montar todas as páginas e baixar o
   resultado pelo botão de download.

Com várias etiquetas distintas ainda não cacheadas, uma barra de progresso
("Renderizando etiqueta X de N...") acompanha a renderização — o rate
limiting descrito acima pode deixar esse passo mais lento do que uma
chamada por etiqueta, então a barra evita a impressão de que o app travou.
Se alguma etiqueta falhar mesmo após as tentativas automáticas, nenhum PDF
é gerado e uma mensagem indica exatamente qual etiqueta falhou.

O cache local de imagens renderizadas (`cache/`) é compartilhado entre a
CLI e a interface web, e cada etiqueta distinta é renderizada só uma vez
(por hash do seu próprio conteúdo ZPL) — repetições e reaproveitamentos da
prévia nunca disparam chamadas de API redundantes.

## Deploy no Vercel

Streamlit exige um processo de servidor persistente com WebSocket, que o
Vercel (modelo serverless) não suporta. Por isso, para publicar no Vercel, a
interface é uma reescrita independente — HTML/CSS/JS estático em
[`public/`](public/) + uma API serverless Python em [`api/index.py`](api/index.py) —
que reutiliza a mesma orquestração de [`zpl2pdf/webapi.py`](zpl2pdf/webapi.py)
usada pela UI Streamlit (nenhuma lógica de parsing/renderização/layout é
duplicada; só a apresentação muda). O `app.py`/Streamlit continua funcionando
normalmente para quem preferir rodar localmente ou hospedar em outra
plataforma (Streamlit Community Cloud, Render, Railway, Fly.io, ...).

### Como funciona

- `api/index.py`: um único app Flask (padrão comum de "Flask on Vercel")
  expõe `POST /api/count` (conta etiquetas), `POST /api/analyze` (grade/escala,
  sem chamar o Labelary), `POST /api/preview` (PNG da 1ª página) e
  `POST /api/generate` (PDF final). `vercel.json` reescreve todo tráfego de
  `/api/*` para esse arquivo; o Flask cuida do roteamento interno.
- `public/index.html` + `public/app.js` + `public/style.css`: formulário
  equivalente à sidebar do Streamlit, chamando os endpoints acima via
  `fetch` — exibe a prévia numa `<img>` e dispara o download do PDF.
- O cache por hash de conteúdo ZPL usa `/tmp` (único diretório gravável em
  funções Vercel) — ainda evita renderizar duas vezes a mesma etiqueta
  **dentro de uma mesma requisição**, mas não persiste de forma garantida
  entre requisições/cold starts.

### Publicar

```powershell
npm install -g vercel   # se ainda não tiver o CLI
vercel login
vercel                  # deploy de preview
vercel --prod           # deploy de produção
```

Ou conecte o repositório GitHub ao Vercel pelo dashboard (Import Project) —
o Vercel detecta `vercel.json` e `api/index.py` automaticamente. A Vercel
instala as dependências Python a partir do [`requirements.txt`](requirements.txt)
da **raiz** do projeto (não de um `requirements.txt` dentro de `api/` — a
função só encontra o da raiz), por isso ele é mantido enxuto (Flask,
reportlab, Pillow, requests, PyYAML) e Streamlit/pytest ficam à parte em
[`requirements-dev.txt`](requirements-dev.txt), só para desenvolvimento local.
`vercel.json` também declara `includeFiles: "zpl2pdf/**"` para garantir que
o pacote `zpl2pdf/` (fora da pasta `api/`) vá junto no bundle da função.

### Limitações do modelo serverless

- **Timeout de função**: arquivos com muitas etiquetas distintas ainda não
  cacheadas podem demorar mais do que o timeout do plano Vercel, por causa
  do rate limiting de ~2 req/s ao Labelary (ex: 60 etiquetas distintas ≈ 30s
  só de renderização, sem contar retries). `vercel.json` já configura
  `maxDuration: 60` — ajuste para o teto do seu plano se precisar de mais
  margem. Para arquivos muito grandes, considere a UI Streamlit (sem
  timeout) ou um Labelary self-hosted via Docker.
- **Tamanho do corpo da requisição**: funções Vercel têm um limite de
  tamanho de upload bem menor do que arquivos ZPL com muitas imagens
  embutidas (ex: o padrão `~DG`/`^XG` descrito nas limitações do parser mais
  abaixo) podem atingir.
- **Sem progresso em tempo real**: a versão web não tem a barra de progresso
  por etiqueta que o Streamlit tem — funções Python clássicas no Vercel não
  fazem streaming de resposta, então só um spinner genérico com estimativa
  de tempo é mostrado durante a renderização.
- **Sem autenticação**: o formulário fica publicamente acessível a quem
  tiver a URL — adicione sua própria camada de autenticação/proteção se
  isso for uma preocupação para o seu caso de uso.

## Argumentos da CLI

| Argumento | Descrição | Default |
|---|---|---|
| `--input` | Caminho do arquivo `.zpl` de entrada (1 ou várias etiquetas; obrigatório) | — |
| `--output` | Caminho do PDF de saída (obrigatório) | — |
| `--config` | Arquivo `.yaml`/`.yml`/`.json` com os parâmetros abaixo | — |
| `--sheet-size` | Preset (`A4`, `Letter`) ou `LARGURAxALTURA` em mm (ex: `100x150`) | `A4` |
| `--orientation` | `portrait` ou `landscape` | `portrait` |
| `--labels-per-page` | Etiqueta única: cópias por página. Múltiplas + `paginate`: etiquetas por página (páginas calculadas automaticamente) | `1` |
| `--pages` | Quantas páginas gerar — só se aplica a arquivo com etiqueta única (ignorado com múltiplas) | `1` |
| `--multi-mode` | **Obrigatório** se o arquivo tiver múltiplas etiquetas: `fit-one-page` ou `paginate` | — |
| `--margin-top/bottom/left/right` | Margens da folha em mm | `10.0` |
| `--gap-x` / `--gap-y` | Espaçamento entre etiquetas em mm | `2.0` |
| `--dpi` | Resolução da impressora (203, 300, ...) | `203` |
| `--label-width` / `--label-height` | Tamanho em mm, usado só se o ZPL não definir `^PW`/`^LL` | — |
| `--stretch` | Estica a etiqueta para preencher 100% da célula (ignora a proporção original) | desligado (mantém proporção, centralizada) |
| `--cache-dir` | Pasta do cache local de imagens renderizadas | `cache` |
| `--no-cache` | Desativa o cache | desligado |
| `-v`, `--verbose` | Log detalhado (debug) | desligado |

Ao final, a CLI imprime um resumo: a grade calculada, a escala aplicada
(ou a pior escala, no caso de múltiplas etiquetas com tamanhos diferentes),
o número de páginas geradas, o total de etiquetas no PDF e o caminho do
PDF final.

## Como funciona o cálculo de dpmm/escala

A API do Labelary espera a densidade de impressão em **dpmm** (dots por
milímetro), enquanto impressoras térmicas normalmente são especificadas em
**dpi** (dots por polegada). A conversão usada é:

```
dpmm_ideal = dpi / 25.4
```

Como o Labelary só aceita um conjunto discreto de valores (6, 8, 12, 24
dpmm), dpis comuns são mapeados diretamente para o dpmm nativo da
impressora (203dpi → 8dpmm, 300dpi → 12dpmm, 600dpi → 24dpmm, 152dpi →
6dpmm). Se um `--dpi` fora desses presets for informado, o valor é
arredondado para o dpmm suportado mais próximo, com um aviso no console
explicando a aproximação.

O **tamanho físico da etiqueta em mm** é obtido de duas formas:

1. Se a etiqueta ZPL define `^PW<dots>` e `^LL<dots>`, o tamanho é
   `dots / dpmm` (ex: `^PW240^LL160` a 8dpmm = 30mm x 20mm).
2. Caso contrário, usa-se o fallback `--label-width`/`--label-height`
   informado manualmente (em mm).

Esse tamanho em mm alimenta diretamente `calculate_grid` (que grade cols x
rows melhor acomoda `labels_per_page` células) e é usado para pedir a
imagem já nesse tamanho ao Labelary (parâmetro `{width}x{height}` da URL,
em polegadas). Depois, `LayoutEngine` calcula o tamanho de cada célula
(área útil da folha menos margens/gaps, dividida pela grade) e redimensiona
a imagem renderizada para caber nela — mantendo a proporção original
(centralizada) por padrão, ou esticando para 100% da célula com
`--stretch`. O desenho final no PDF via `reportlab` sempre usa mm reais
convertidos para pontos, na escala calculada.

## Arquitetura

```
app.py                    # interface web (Streamlit) — reusa webapi.py
public/                   # frontend estático da versão Vercel (HTML/CSS/JS)
api/index.py              # API serverless (Vercel) — Flask, reusa webapi.py
zpl2pdf/
├── parser.py       # separa etiquetas ^XA...^XZ e extrai ^PW/^LL/^JM
├── renderer.py       # LabelRenderer (interface) + LabelaryRenderer + cache em disco
├── layout.py          # calculate_grid + LayoutEngine (place_single / place_sequence)
├── pdf_builder.py     # desenha as etiquetas no PDF final via reportlab
├── config.py           # SheetConfig/PrintConfig, presets de folha, erros de config
├── webapi.py            # orquestração compartilhada por app.py e api/index.py
└── cli.py               # argparse + orquestração ponta a ponta (própria, não usa webapi.py)
```

Fluxo de dados: `.zpl` → `parser` separa em `List[ZplLabel]` (1 ou N blocos)
→ tamanho original em mm resolvido por etiqueta (ZPL ou fallback) →
`calculate_grid(grid_count, sheet, ref_size)` calcula `(cols, rows)` com
`cols x rows == grid_count` → cada etiqueta distinta é renderizada **uma
única vez**, no seu tamanho original (`RenderedLabel`, via Labelary +
cache por hash do ZPL) → `LayoutEngine` calcula o tamanho de célula da
grade e redimensiona cada imagem para caber nela (proporcional ou
esticada) → `pdf_builder` desenha tudo no PDF.

O que muda entre os três cenários é só `grid_count` (o que dimensiona a
grade) e qual método do `LayoutEngine` preenche as células:

| Cenário | `grid_count` | Método |
|---|---|---|
| Etiqueta única | `labels_per_page` | `place_single(rendered, pages)` — repete a mesma imagem em toda a grade, por N páginas |
| Múltiplas, caber em 1 página | `len(labels)` | `place_sequence(rendered_labels)` — uma lista de N imagens distintas, 1 por célula, N == capacidade da grade → 1 página |
| Múltiplas, paginar | `labels_per_page` | `place_sequence(rendered_labels)` — mesma lista de N imagens; como N pode exceder a capacidade de uma página, transborda automaticamente para páginas seguintes (a última, parcial, usa o mesmo tamanho de célula) |

`LabelRenderer` é uma interface abstrata justamente para permitir trocar o
`LabelaryRenderer` por um renderizador offline (ex: baseado em uma
biblioteca ZPL local) no futuro, sem alterar parser, layout ou CLI.

### Por que reportlab (e não fpdf2)

`reportlab` foi escolhido por ter controle mais preciso de unidades
(mm/pontos), aceitar imagens diretamente de bytes em memória via
`ImageReader` (sem precisar salvar PNGs temporários em disco, reaproveitado
entre todas as repetições da mesma etiqueta) e por sua API de canvas/
paginação ser mais direta para desenhar em coordenadas e tamanhos
absolutos exatos — necessário já que o tamanho de desenho de cada etiqueta
é calculado explicitamente por `LayoutEngine` (célula da grade, com ou sem
preservar proporção).

## Testes

```powershell
python -m pytest tests/ -v
```

Cobrem o parser (separação de etiquetas `^XA...^XZ` — detecção de arquivo
com 1 vs. N blocos —, extração de `^PW`/`^LL`/`^JM`, tratamento de arquivo
malformado/inexistente) e o layout/grade:

- `compute_cell_size_mm` / `compute_draw_size_mm`: tamanho de célula a
  partir da área útil, margens e gap; redimensionamento preservando
  proporção vs. modo `stretch`.
- `calculate_grid`: quantidade exata, grades equilibradas vs. degeneradas,
  números primos, quantidades grandes que antes "não cabiam" e agora nunca
  levantam erro.
- `LayoutEngine.place_single` (Cenário 1): repetição da mesma etiqueta
  entre páginas, ausência de sobreposição entre células.
- `LayoutEngine.place_sequence` (Cenários 2 e 3): grade dimensionada para
  caber tudo em 1 página, paginação com `ceil(N / labels_per_page)`
  (incluindo a última página parcial), **a última página usando o mesmo
  tamanho de célula das páginas cheias** (posições sobrando ficam em
  branco, não recalculadas para uma grade menor), e redimensionamento
  independente por etiqueta quando os tamanhos originais diferem entre si.

## Exemplos incluídos

- [`examples/label_30x20mm.zpl`](examples/label_30x20mm.zpl) — 1 etiqueta
  pequena (30x20mm, `^PW240^LL160` a 203dpi), texto + código de barras.
- [`examples/label_barcode_simples.zpl`](examples/label_barcode_simples.zpl) —
  1 etiqueta com `^PW`/`^LL` definidos (50.75x25.375mm), texto + código de barras.
- [`examples/label_texto.zpl`](examples/label_texto.zpl) — 1 etiqueta **sem**
  `^PW`/`^LL` (testa o fallback `--label-width`/`--label-height`).
- [`examples/label_multiplas.zpl`](examples/label_multiplas.zpl) — 7
  etiquetas distintas no mesmo arquivo (pedidos #1001 a #1007); use com
  `--multi-mode fit-one-page` ou `--multi-mode paginate --labels-per-page N`.

Teste rápido:

```powershell
python -m zpl2pdf.cli --input examples\label_30x20mm.zpl --output saida.pdf --config config.example.yaml
```

## Limitações conhecidas

- Depende de internet e da disponibilidade da API pública do Labelary para
  renderizar a etiqueta; não há renderizador offline embutido (a interface
  `LabelRenderer` foi desenhada para permitir adicionar um no futuro sem
  alterar o resto do sistema).
- O plano gratuito do Labelary limita a 3 requisições/segundo; arquivos com
  muitas etiquetas distintas (não cacheadas) demoram mais para renderizar
  por causa do rate limiting e do retry com backoff em caso de HTTP 429 (ver
  seção de instalação acima). Para uso intenso, considere um Labelary
  self-hosted via Docker, que não tem esse limite.
- Com múltiplas etiquetas distintas, a escolha de orientação da grade
  (`calculate_grid`) usa o tamanho da **primeira** etiqueta do arquivo como
  referência; cada etiqueta é depois redimensionada individualmente com
  seu próprio tamanho, mas a forma da grade em si (cols x rows) não é
  reotimizada por etiqueta.
- Como a quantidade por página é sempre respeitada redimensionando a
  etiqueta, pedir uma quantidade muito grande para a folha reduz bastante a
  escala (o aviso de <50% avisa sobre isso, mas não impede a geração) —
  cabe ao usuário escolher uma quantidade compatível com a legibilidade
  física desejada do código de barras.
- O `^JM` (modo de dots-per-millimeter do próprio ZPL) é extraído pelo
  parser mas não é usado para alterar o dpmm da renderização — quem define
  o dpmm é sempre o `--dpi` configurado, correspondendo à impressora alvo.

## Etiquetas com imagem armazenada (`~DG`/`^XG`)

Alguns exportadores (ex: Zebra Setup Utilities/drivers de impressora) geram
arquivos onde cada etiqueta é precedida por um comando `~DG` (download de
imagem) **fora** de qualquer bloco `^XA...^XZ`, seguido por um bloco que a
recupera e imprime via `^XG`, e por um bloco adicional `^XA...^XZ` contendo
só `^ID` (image delete) para limpar a imagem da memória — sem desenhar nada.
O parser (`split_labels` em [`zpl2pdf/parser.py`](zpl2pdf/parser.py)) trata
esse caso automaticamente: mantém o `~DG` junto do bloco de impressão que o
usa (senão o Labelary recebe o `^XG` sem a imagem correspondente e retorna
"ZPL generated no labels") e descarta os blocos que só contêm `^ID`, por não
terem conteúdo visual.
