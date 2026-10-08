# core/graph_relatorio_email.py — baixa o relatório do Graph, busca datas de criação e envia o e-mail HTML
import requests
import csv
import io
import base64
import os
import html


def get_email_activity_report(token):
    headers = {"Authorization": f"Bearer {token}"}
    
    # NOVO ENDPOINT: Traz a atividade + licenças (Assigned Products) no mesmo arquivo
    url = "https://graph.microsoft.com/v1.0/reports/getEmailActivityUserDetail(period='D180')"
    
    print("Baixando Relatório Unificado (Atividade + Licenças)...")
    resposta = requests.get(url, headers=headers)
    
    if resposta.status_code != 200:
        raise Exception(f"Erro ao buscar relatório: {resposta.text}")
        
    conteudo_csv = resposta.content.decode('utf-8-sig') 
    leitor = csv.DictReader(io.StringIO(conteudo_csv))
    return list(leitor)


def get_dados_entra(token):
    """Retorna {UPN em minúsculas: {"criada_em": data ISO, "login_ativo": bool}} de todos os usuários do Entra ID.
    Usa a permissão User.Read.All (já coberta pelo User.ReadWrite.All)."""
    headers = {"Authorization": f"Bearer {token}"}
    url = ("https://graph.microsoft.com/v1.0/users"
           "?$select=userPrincipalName,createdDateTime,accountEnabled&$top=999")
    dados = {}
    while url:
        resposta = requests.get(url, headers=headers)
        if resposta.status_code != 200:
            raise Exception(f"Erro ao buscar dados do Entra ID: {resposta.status_code} - {resposta.text[:200]}")
        pagina = resposta.json()
        for u in pagina.get("value", []):
            if u.get("userPrincipalName"):
                dados[u["userPrincipalName"].lower()] = {
                    "criada_em": u.get("createdDateTime"),
                    "login_ativo": u.get("accountEnabled"),
                }
        url = pagina.get("@odata.nextLink")
    return dados


def tabela_bloqueios_html(df_res):
    """Bloco HTML para inserir no corpo do e-mail."""
    if df_res is not None and not df_res.empty:
        df_res = df_res[df_res["Resultado"] != "JA_BLOQUEADA"]  # não repete conta antiga no e-mail
    if df_res is None or df_res.empty:
        return "<p>Nenhuma conta foi bloqueada nesta execução.</p>"

    cores = {"BLOQUEADA": "#c0392b", "CONVERTIDA": "#c0392b", "LICENCA_REMOVIDA": "#c0392b",
             "SIMULADO": "#7f8c8d", "PULADA": "#e67e22", "ERRO": "#8e44ad"}
    linhas = "".join(
        f"<tr><td>{r.Email}</td><td>{r.Tipo_Caixa}</td><td>{r.Acao}</td>"
        f"<td style='color:{cores.get(r.Resultado, '#000')};font-weight:bold'>{r.Resultado}</td>"
        f"<td>{r.Detalhe}</td></tr>"
        for r in df_res.itertuples()
    )
    ok = df_res["Resultado"].isin(["BLOQUEADA", "CONVERTIDA", "LICENCA_REMOVIDA"]).sum()
    n_sim = (df_res["Resultado"] == "SIMULADO").sum()
    if n_sim and ok == 0:
        titulo = f"Simulação (dry-run): {n_sim} contas SERIAM processadas — nada foi alterado"
    else:
        titulo = f"Contas bloqueadas nesta execução ({ok} de {len(df_res)})"
    return (
        f"<h3>{titulo}</h3>"
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;font-family:Segoe UI,Arial;font-size:13px'>"
        "<tr style='background:#f2f2f2'><th>Conta</th><th>Tipo</th><th>Ação</th>"
        "<th>Resultado</th><th>Detalhe</th></tr>"
        f"{linhas}</table>"
    )


def _anexo_csv(caminho):
    with open(caminho, "rb") as arquivo:
        conteudo_base64 = base64.b64encode(arquivo.read()).decode("utf-8")
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": os.path.basename(caminho),
        "contentType": "text/csv",
        "contentBytes": conteudo_base64
    }


def _enviar_graph(token, remetente, dados_email):
    url = f"https://graph.microsoft.com/v1.0/users/{remetente}/sendMail"
    return requests.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json=dados_email)


def enviar_email_aviso(token, remetente, destinatario, assunto, mensagem):
    """Envia um e-mail curto de alerta (por exemplo, quando a execução foi cancelada por erro)."""
    corpo = (f"<h2 style='color: #c0392b;'>{html.escape(assunto)}</h2>"
             f"<p>{html.escape(str(mensagem))}</p>")
    resposta = _enviar_graph(token, remetente, {
        "message": {"subject": assunto, "body": {"contentType": "HTML", "content": corpo},
                    "toRecipients": [{"emailAddress": {"address": destinatario}}]},
        "saveToSentItems": "false",
    })
    if resposta.status_code == 202:
        print("✅ E-mail de alerta enviado.")
    else:
        print(f"❌ Erro ao enviar e-mail de alerta: {resposta.status_code} - {resposta.text}")


def enviar_email_resumo(token, remetente, destinatario, caminho_arquivo, df_final,
                        df_bloqueios=None, caminho_bloqueios=None,
                        erro_bloqueio=None, dry_run=True):
    """Envia o resumo em HTML. Anexa o CSV de pendências (se houver) e o CSV do que foi processado.
    Sem candidatas, o e-mail informa '0 caixas inativas para efetuar a automação'."""
    print(f"Preparando envio de e-mail para {destinatario}...")

    # Calcula os dados para o resumo do e-mail
    classif = df_final['Classificacao']
    total_candidatas = int((classif == 'CANDIDATA A BLOQUEIO').sum())
    total_ativas = int((classif == 'ATIVA').sum())
    total_so_bloqueio = int((df_final['Estagio'] == 'BLOQUEAR').sum())
    total_completo = int((df_final['Estagio'] == 'CICLO COMPLETO').sum())
    total_recentes = int((classif == 'CONTA RECENTE').sum())
    total_revisar = int(classif.str.startswith('REVISAR').sum())
    total_ja_bloqueadas = int(classif.str.startswith('BLOQUEADA').sum())

    if total_candidatas == 0:
        # Nada a processar nesta execução
        bloco_principal = (
            "<div style='padding: 12px; background-color: #e8f5e9; border: 1px solid #c8e6c9; border-radius: 4px;'>"
            "<b>✅ 0 caixas inativas para efetuar a automação nesta execução.</b></div>"
        )
        linha_candidatas = "<li><b>Candidatas a Bloqueio:</b> 0 caixas</li>"
    else:
        # Pega as 10 contas inativas há mais tempo para destacar no corpo do e-mail
        df_candidatas = df_final[classif == 'CANDIDATA A BLOQUEIO'].sort_values(by='Dias_Inativa', ascending=False)
        linhas_tabela_html = ""
        for _, row in df_candidatas.head(10).iterrows():
            linhas_tabela_html += f"<tr><td style='border: 1px solid #ddd; padding: 8px;'>{row['Email']}</td><td style='border: 1px solid #ddd; padding: 8px;'>{row['Dias_Inativa']} dias</td><td style='border: 1px solid #ddd; padding: 8px;'>{row['Licenças']}</td></tr>"

        # Bloco de bloqueios (simulação ou execução real)
        bloco_bloqueios = ""
        if erro_bloqueio:
            bloco_bloqueios = (
                "<p style='color: #c0392b;'><b>⚠ O bloqueio automático NÃO foi executado:</b> "
                f"{html.escape(str(erro_bloqueio))}</p>"
            )
        elif df_bloqueios is not None:
            bloco_bloqueios = tabela_bloqueios_html(df_bloqueios)

        bloco_principal = f"""
    {bloco_bloqueios}

    <h3>Top 10 Caixas Inativas a mais tempo (Com Licença Alocada):</h3>
    <table style='border-collapse: collapse; width: 100%; font-family: Arial, sans-serif;'>
        <tr style='background-color: #f2f2f2;'>
            <th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>E-mail</th>
            <th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>Tempo de Inatividade</th>
            <th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>Licenças Consumidas</th>
        </tr>
        {linhas_tabela_html}
    </table>
    <br>"""
        linha_candidatas = (f"<li><b>Candidatas a Bloqueio:</b> {total_candidatas} caixas "
                            f"(só bloqueio do login: {total_so_bloqueio} · ciclo completo, com recuperação de licença: {total_completo})</li>")

    anexos = []
    if caminho_arquivo and os.path.exists(caminho_arquivo):
        anexos.append(_anexo_csv(caminho_arquivo))
        frase_csv = "<p>As pendências (candidatas e contas para revisão manual) estão no arquivo <b>CSV em anexo</b>.</p>"
    else:
        frase_csv = ""
    if caminho_bloqueios and os.path.exists(caminho_bloqueios):
        anexos.append(_anexo_csv(caminho_bloqueios))

    corpo_html = f"""
    <h2>Relatório de Conformidade de Contas - Microsoft 365</h2>
    <p>A automação finalizou a varredura nas caixas de e-mail e cruzou os dados de inatividade, licenças e tipos de caixa (ignorando caixas de sistema e compartilhadas sem licenças).</p>

    {bloco_principal}

    <h3 style='color: #d9534f;'>Resumo Geral:</h3>
    <ul>
        {linha_candidatas}
        <li><b>Já bloqueadas, aguardando o prazo de conversão:</b> {total_ja_bloqueadas} caixas</li>
        <li><b>Caixas Ativas:</b> {total_ativas} caixas</li>
        <li><b>Contas recentes, ainda sem uso (aguardando):</b> {total_recentes} caixas</li>
        <li><b>Para revisão manual:</b> {total_revisar} caixas</li>
    </ul>
    {frase_csv}
    """

    # Assunto: deixa claro quando é simulação e quantas contas foram de fato bloqueadas
    prefixo = "[SIMULAÇÃO] " if dry_run else ""
    if total_candidatas == 0:
        assunto = f"{prefixo}Automação M365: 0 caixas inativas para processar"
    else:
        assunto = f"{prefixo}Aviso de Otimização M365: {total_completo} Licenças podem ser recuperadas"
        if df_bloqueios is not None and not df_bloqueios.empty and not dry_run:
            n_bloqueadas = int(df_bloqueios["Resultado"].isin(["BLOQUEADA", "CONVERTIDA", "LICENCA_REMOVIDA"]).sum())
            if n_bloqueadas:
                assunto += f" ({n_bloqueadas} bloqueadas nesta execução)"

    mensagem = {
        "subject": assunto,
        "body": {"contentType": "HTML", "content": corpo_html},
        "toRecipients": [{"emailAddress": {"address": destinatario}}],
    }
    if anexos:
        mensagem["attachments"] = anexos

    resposta = _enviar_graph(token, remetente, {"message": mensagem, "saveToSentItems": "false"})

    if resposta.status_code == 202:
        print("✅ E-mail com resumo enviado com sucesso!")
    else:
        print(f"❌ Erro ao enviar e-mail: {resposta.status_code} - {resposta.text}")
