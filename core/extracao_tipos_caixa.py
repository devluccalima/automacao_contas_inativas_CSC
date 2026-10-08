"""core/extracao_tipos_caixa.py — roda scripts/extrai_tipos_caixa.ps1 antes de cada execução e valida o CSV gerado.

Assim a classificação sempre usa os tipos de caixa atuais (UserMailbox, SharedMailbox...).
"""
import os
import subprocess
import time

import pandas as pd

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PS = os.path.join(RAIZ, "scripts", "extrai_tipos_caixa.ps1")
CAMINHO_CSV = os.path.join(RAIZ, "config", "tipos_caixa_exchange.csv")


def atualizar_tipos_caixa(timeout=900):
    """Roda a extração no Exchange e confirma que o CSV foi regravado agora.
    Retorna o número de caixas extraídas. Levanta RuntimeError se algo falhar."""
    if not os.path.exists(SCRIPT_PS):
        raise RuntimeError(f"Script não encontrado: {SCRIPT_PS}")

    ps = os.getenv("POWERSHELL_EXE", "powershell.exe")
    inicio = time.time()
    proc = subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT_PS],
        capture_output=True, text=True,
        encoding="oem" if os.name == "nt" else "utf-8", errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Extração do Exchange falhou (código {proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[-400:]}")

    if not os.path.exists(CAMINHO_CSV) or os.path.getmtime(CAMINHO_CSV) < inicio - 2:
        raise RuntimeError("O CSV de tipos de caixa não foi atualizado pela extração.")

    total = len(pd.read_csv(CAMINHO_CSV, sep=";"))
    if total == 0:
        raise RuntimeError("O CSV de tipos de caixa ficou vazio.")
    return total
