# Como instalar o Estoque SPED

Passo a passo para instalar o Estoque SPED em qualquer computador com Windows.

## Antes de começar

| Precisa | Detalhe |
|---|---|
| Windows 10 ou 11, 64 bits | quase todo computador atual é 64 bits |
| Uns 400 MB livres no disco | o instalador tem 125 MB |
| Internet só para baixar | depois, o programa funciona sem internet (só a busca automática do CNPJ precisa dela) |

**Não precisa** de senha de administrador, nem de instalar Python, Edge ou Chrome. Tudo vem dentro do instalador.

## 1. Baixar o instalador

1. Abra a página **https://github.com/gilmariolima/estoque-sped/releases/latest**
2. Lá embaixo, em **Assets**, clique em **`Estoque-SPED-Setup-<versão>.exe`**.
3. O arquivo vai para a pasta **Downloads**.

> O navegador pode avisar que o arquivo "não é baixado com frequência". No Edge, clique nos três pontinhos do aviso e em **Manter**. No Chrome, clique em **Manter**.

Sem internet no computador? Baixe em outro e leve o `.exe` num pendrive.

## 2. Instalar

1. Abra a pasta **Downloads** e dê **dois cliques** no `Estoque-SPED-Setup-<versão>.exe`.
2. Se aparecer a tela azul **"O Windows protegeu o computador"**, clique em **Mais informações** e depois em **Executar assim mesmo**.
   O aviso aparece porque o programa não tem assinatura digital paga; ele não indica vírus.
3. A instalação é automática e não faz perguntas. Em alguns segundos, o Estoque SPED abre sozinho.

Ficam dois atalhos **Estoque SPED**: um na **Área de Trabalho** e outro no **Menu Iniciar**. Para fixar na barra de tarefas, clique com o botão direito no ícone com o programa aberto e escolha **Fixar na barra de tarefas**.

## 3. Primeiro uso (uma vez só)

1. Abra a aba **Escritório**.
2. Preencha os dados do contador: **nome, CPF, CRC, e-mail e município (código IBGE)**. Os outros campos são opcionais.
3. Clique em **Salvar**.

Sem isso, os arquivos saem sem o registro 0100 (contabilista), e o validador da Receita (PVA) recusa.

## 4. Usar

1. Na aba **Converter**, arraste os arquivos do cliente para a área **"Solte aqui os arquivos do cliente"**: relatório de inventário, `.FS` da Fortes, bloco H ou planilha.
2. Confira os **Dados do arquivo**. O que estiver em vermelho falta preencher (CNPJ, inscrição estadual, UF, município).
3. Veja os itens com **Pendência** e corrija o que for preciso.
4. Clique em **Baixar SPED** (para o PVA) ou em **Baixar para o Domínio**.

Os arquivos gerados também ficam na aba **Arquivos gerados**.

## Atualizar para uma versão nova

A partir da versão 1.2.0 é automático: ao abrir, o Estoque SPED baixa a versão nova sozinho e pergunta se quer **Reiniciar agora**. Se escolher **Depois**, ela é instalada quando você fechar o programa.

Sem internet, ou numa versão anterior à 1.2.0: baixe o instalador novo (passo 1) e instale por cima (passo 2). O instalador fecha o Estoque SPED aberto, troca o programa e **mantém** os cadastros das empresas, os dados do escritório e os arquivos gerados.

## Onde ficam os dados

Os dados ficam na pasta abaixo. Para abrir, cole o caminho na barra de endereço do Explorador de Arquivos:

```
%LOCALAPPDATA%\Estoque SPED\dados
```

| Arquivo ou pasta | O que é |
|---|---|
| `empresas.json` | cadastro das empresas (IE, UF, município, perfil) |
| `escritorio.json` | dados do contador |
| `saida\` | SPEDs e arquivos do Domínio gerados |
| `painel.log` | registro de erros, para suporte |

**Levar os cadastros para outro computador:** instale o Estoque SPED no computador novo, feche o programa e copie a pasta `dados` do computador antigo por cima da nova.

## Desinstalar

**Configurações → Aplicativos → Aplicativos instalados → Estoque SPED → Desinstalar.**

A pasta `dados` não é apagada, para não perder os cadastros. Para remover tudo, apague também a pasta `%LOCALAPPDATA%\Estoque SPED`.

## Problemas comuns

| Problema | O que fazer |
|---|---|
| O antivírus da empresa bloqueia o instalador | Peça ao TI para liberar o arquivo `Estoque-SPED-Setup-<versão>.exe`. |
| O programa não abre | Feche pelo Gerenciador de Tarefas qualquer "Estoque SPED" que tenha ficado aberto e abra de novo pelo atalho. Se continuar, mande o arquivo `painel.log` da pasta `dados`. |
| Ao abrir de novo, nada acontece | O programa já está aberto: procure a janela na barra de tarefas. |
| Computador tinha a versão antiga (zip com `Instalar.cmd`) | Instale normalmente. Na primeira vez, o programa passa os dados da versão antiga para a nova e apaga o que sobrou dela. |
