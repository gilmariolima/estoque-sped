# Estoque SPED

Converte o inventário do cliente em **SPED Fiscal (EFD ICMS/IPI) com o bloco H**, pronto para importar no PVA.

**Para instalar em qualquer computador:** baixe o instalador na [última versão](https://github.com/gilmariolima/estoque-sped/releases/latest) e siga o passo a passo em [INSTALAR.md](INSTALAR.md).

Entradas aceitas (o tipo é reconhecido pelo conteúdo):

- relatório **Registro de Inventário** em texto (colunas de largura fixa);
- arquivo **.FS da Fortes** (registros CAB/GRP/UND/PRO/IIV);
- **bloco H** solto ou SPED completo (`.txt` com `|H010|`);
- **planilhas** `.xls`, `.xlsx`, `.csv` (Planilha Modelo, Estoque Escriturado ou parecidas).

O primeiro arquivo com valores é o inventário; os demais só completam descrição, NCM, unidade, tipo do item, conta contábil e custo unitário (e, se trouxerem quantidade diferente, isso aparece nas pendências). O que não dá para resolver sozinho vai para o CSV de pendências.

Cada conversão gera dois arquivos:

| Arquivo | Para quê |
|---|---|
| `SPED_<CNPJ>_<data>.txt` | EFD ICMS/IPI com o bloco H, no leiaute da Receita, para o PVA |
| `DOMINIO_<CNPJ>_<data>.txt` | o inventário no leiaute do `produtos.txt` da Planilha Modelo (sem cabeçalho, separado por `;`: código, descrição, unidade, NCM, CEST, conta contábil, quantidade, valor unitário, valor total, ICMS recuperável), para importar no Domínio |

Os dois saem do mesmo inventário e têm o mesmo total.

## Instalar como app do Windows

O Estoque SPED é um programa de verdade (feito em Electron, como o VS Code), com ícone próprio na Área de Trabalho, no Menu Iniciar e na barra de tarefas. O instalador é de um clique e não precisa de Python, Edge nem Chrome no computador do escritório.

**Gerar o instalador** (uma vez por versão, num Windows 64 bits com Node 22+ e internet, dentro da pasta do código):

```powershell
npm ci
npm run pacote
```

Sai em `pacote\Estoque-SPED-Setup-<versão>.exe`. Para uma versão nova, aumente o `version` do `package.json` antes.

O `npm run pacote` primeiro monta o Python que vai dentro do app (`app\python`: o Python 3.12 *embeddable* oficial de python.org com openpyxl, et_xmlfile e xlrd do PyPI, conferidos pelo sha256). Isso só é baixado na primeira vez. Sem internet, dá para usar um zip já baixado: `npm run preparar-python -- --zip python-3.12.10-embed-amd64.zip`.

**Instalar** no computador do escritório: dois cliques no `Estoque-SPED-Setup-<versão>.exe`. Ele instala sem perguntar nada e já abre o Estoque SPED.

Como o app funciona:

- abre numa janela própria; o "motor" (o conversor em Python) roda escondido numa porta livre do próprio computador e só aceita pedidos da janela do app;
- "Baixar SPED" pergunta onde salvar, sugerindo a pasta Downloads;
- fechar a janela fecha o app (não fica nada rodando em segundo plano);
- abrir o app de novo com ele já aberto só traz a janela para a frente.

**Dados:** ficam em `%LOCALAPPDATA%\Estoque SPED\dados` (`empresas.json`, `escritorio.json`, `saida\` com os SPEDs gerados, `entradas\` e `painel.log`), fora da pasta do programa. Atualizar ou desinstalar **nunca** apaga os dados.

**Atualizar:** rode o instalador da versão nova. Ele fecha o Estoque SPED aberto, troca o programa e mantém os dados.

**Vindo da versão antiga** (zip com `Instalar.cmd` e janela do Edge): é só instalar. Na primeira vez o app desliga o painel antigo, move a pasta `%LOCALAPPDATA%\Programs\Estoque SPED\dados` para `%LOCALAPPDATA%\Estoque SPED\dados` e só então apaga o que a versão antiga deixou (`Estoque SPED.cmd`, `LEIA-ME.txt` e a pasta `app`). Os atalhos "Estoque SPED" têm o mesmo nome e são substituídos pelo instalador. Se a pasta nova já tiver dados, nada é movido nem apagado.

## Desenvolvimento

```powershell
npm install
npm run preparar-python   # só no Windows; fora dele o app usa o python3 do sistema
npm run app               # abre o app a partir do código
npm test                  # testes do app (Node) e do conversor (Python)
```

O conversor também roda sozinho, pela linha de comando (com Python e `pip install -r app/requirements.txt`):

```powershell
cd app
python gerar_sped.py inventario.txt planilha.xlsx --data-inv 31/12/2025 --periodo 02/2026
python gerar_sped.py --help
```

Estrutura:

| Pasta | O que tem |
|---|---|
| `app/gerar_sped.py` | leitores dos formatos e montagem do arquivo (leiaute pelo Guia Prático EFD ICMS/IPI) |
| `app/painel.py`, `app/painel/` | o painel local (servidor HTTP em `127.0.0.1` e a página) |
| `app/tests/` | testes do conversor e do painel |
| `electron/` | o app desktop: janela, motor, pasta de dados e migração da versão antiga |
| `scripts/` | montagem do Python embutido |
| `test/` | testes do app |

## Modelo de planilha para o cliente

`modelos/Modelo_Inventario_Estoque_SPED.xlsx` é a planilha para mandar ao cliente quando ele não tem um relatório de inventário: empresa, CNPJ e data no topo (o app lê dali), uma linha por item com código, descrição, unidade, NCM, CEST, conta contábil, quantidade, valor unitário (o total é calculado) e ICMS recuperável, uma aba de instruções e uma de exemplo. Preenchida, é só arrastar no Estoque SPED. Para mudar o modelo, edite `modelos/gerar_modelo.py` e rode de novo.

## Arquivos de clientes

O conversor foi conferido com inventários reais (relatórios de inventário de matriz e filial com quase 8 mil itens, um `.FS` da Fortes, Estoque Escriturado e a Planilha Modelo): o total do SPED bateu com o total do relatório, a não ser quando o próprio relatório arredonda o total (aí vale a soma das linhas, que é o que o PVA exige no H005, e a diferença aparece nas pendências). Os casos que esses arquivos mostraram estão em `app/tests/arquivos` como cópias sintéticas, sem dados de cliente.

No relatório de inventário o código e a descrição vêm na mesma coluna. O app separa pelos primeiros termos com número ou barra (`2022GSG/03 RELOGIO ...`), aceita um prefixo curto de letras (`BG 99100-003-A`, `GC 1999885`) e tira o traço entre os dois (`5095 - EXPOSITOR`). O "P/" de "para" fica na descrição, mesmo tendo barra (`PU11599JS/02 PULS.P/REL. PHALANX`). Sem número no começo, o código é o que vem antes de " - " (`BIP LITE BLACK - SMARTWATCH`) ou um termo curto com hífen (`MW-CX CAIXA EM ACO`). Códigos com espaço separados com segurança geram um aviso só para o arquivo; vão para as pendências do item os casos duvidosos (código antes do " - ", código grudado numa palavra, como `CX2038GSG/01CAIXA`). Linha sem código vira `SEMCOD0001`... e código repetido com outra descrição vira `<código>-2`: os dois casos também vão para as pendências, para conferir com o cliente.

## Observações fiscais

- O período padrão do arquivo é o **segundo mês depois do inventário**, como manda o Guia Prático: o inventário de 31/12/2025 vai na EFD de 02/2026 (e com o leiaute de 2026). Outro período pode ser informado, e aí uma pendência lembra a regra.
- O arquivo leva só o inventário (bloco H), com a apuração (E110) zerada: não transmita como substituto de uma EFD que tem notas e apuração.
- Só o motivo 01 (final do período) é gerado: os motivos 02 a 06 exigem o H020/H030 (CST, base e ICMS de cada item), que os arquivos de inventário não trazem.
- Itens em poder de terceiros ou de terceiros em poder do informante (IND_PROP 1 e 2) saem com o COD_PART e o registro 0150 quando o participante é conhecido: pelo 0150 de um SPED de entrada ou por um CNPJ na coluna "Participante"/"CNPJ do terceiro" da planilha (o app consulta nome e município). Sem participante, o item fica fora do arquivo, que continua válido, e a pendência explica o porquê. A coluna "Qtd. em terceiros" vira um H010 próprio com IND_PROP 1.
- Faltando CEP, endereço ou bairro (registro 0005), CRC do contabilista (0100), ou com município de outra UF, o arquivo sai com pendência: o PVA recusaria.

## O que vira pendência

O app nunca muda nem descarta um dado do cliente sem avisar. Vão para as pendências (e para o CSV ao lado do SPED):

- linha sem código (entra como `SEMCOD0001`), linha de relatório que não casa com o formato, linha sem código nem descrição que tem números;
- números ambíguos (`1.500` sem nenhum outro número com casas decimais no arquivo) e planilhas que misturam vírgula e ponto decimal;
- código ou descrição com caractere que o ISO-8859-1 não tem, código cortado em 60 caracteres, unidade cortada em 6;
- TIPO_ITEM fora da tabela, CEST sem 7 dígitos, NCM sem 8 dígitos, ICMS recuperável maior que o valor do item;
- outra aba da planilha com inventário, itens do arquivo de complemento que não estão no inventário, SPED de entrada com mais de um H005.

## Segurança do painel

O painel escuta só em `127.0.0.1` e só atende quem tem o token da execução: o app desktop cria o token, passa ao motor pelo ambiente e abre a janela com ele, e o painel guarda num cookie `HttpOnly`/`SameSite=Strict`. Assim nem um site aberto no navegador nem outro usuário do mesmo computador (servidor de terminal) usa a porta. As respostas levam `Content-Security-Policy` (só scripts do próprio painel), e os nomes de arquivo recebidos nunca saem da pasta do lote ou da pasta `saida`.
