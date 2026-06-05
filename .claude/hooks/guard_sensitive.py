#!/usr/bin/env python3
"""
Garde-fou PreToolUse pour Kairos Alpha (bot de trading LIVE, capital reel).

Recoit le JSON du tool sur stdin et emet une decision de permission :
  - "deny" : action dangereuse interdite (tuer le process du bot live)
  - "ask"  : action sensible -> demande confirmation explicite a l'utilisateur
  - (rien) : laisse passer (exit 0, aucune sortie)

Defensif : toute erreur de parsing -> on laisse passer (exit 0). La couche
`permissions` de settings.json couvre deja les cas critiques ; ce hook est
de la defense en profondeur et ne doit JAMAIS bloquer du travail legitime
par accident (l'utilisateur est souvent absent).
"""
from __future__ import annotations

import json
import re
import sys


def _decision(kind: str, reason: str) -> None:
    """Emet une decision de permission PreToolUse puis sort proprement."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _is_env_file(path: str) -> bool:
    """True pour .env et .env.<variant>, mais PAS .env.example (template public)."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base == ".env":
        return True
    return base.startswith(".env.") and base != ".env.example"


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)  # fail-open

    tool = data.get("tool_name", "")
    ti   = data.get("tool_input", {}) or {}

    # 1) Edition directe du .env via les outils fichier
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        if _is_env_file(str(ti.get("file_path", ""))):
            _decision("ask",
                      ".env contient les cles API Coinbase, COINBASE_MODE=live et le "
                      "sizing/risk. Confirme explicitement avant toute modification.")
        sys.exit(0)

    # 2) Commandes shell
    if tool in ("Bash", "PowerShell"):
        low = str(ti.get("command", "")).lower()

        # 2a) Tuer le process du bot LIVE -> interdit.
        # On exige une vraie INVOCATION (debut de commande, ou apres ; & | `),
        # pas une simple mention dans un message/argument
        # (ex: git commit -m "...taskkill..." ne doit PAS etre bloque).
        if re.search(r"(?:^\s*|[;&|`]\s*)(taskkill|stop-process|pkill)\b", low):
            _decision("deny",
                      "Cette commande peut tuer le process du bot LIVE. "
                      "Si un arret est voulu, fais-le manuellement (Ctrl+C).")

        # 2b) Ecriture/suppression du .env via shell
        touches_env = bool(re.search(r"(^|[\s\"'/\\])\.env(\b|$)", low)) and ".env.example" not in low
        writes = any(tok in low for tok in (
            ">", ">>", "set-content", "add-content", "out-file",
            "remove-item", "del ", "erase ", " rm ", "move-item", "copy-item",
        ))
        if touches_env and writes:
            _decision("ask",
                      "Cette commande shell modifie/supprime .env (secrets + mode live). "
                      "Confirme explicitement.")

        # 2c) Passage explicite en COINBASE_MODE=live (vraie ASSIGNATION, pas une mention)
        if (re.search(r"\$env:\s*coinbase_mode\s*=\s*['\"]?live", low)
                or re.search(r"(?:^\s*|[;&|`]\s*)(set\s+|export\s+)?coinbase_mode\s*=\s*['\"]?live", low)):
            _decision("ask",
                      "Passage en COINBASE_MODE=live (capital reel). Confirme explicitement.")

        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
