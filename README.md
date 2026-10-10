# Reason

**Bounded decisions and continuous improvement for any automation.** Reason is a small, dependency-free Python
service that sits next to your workflows (n8n, Make, Zapier, scripts, LangGraph, Claude Code, OpenAI Agents SDK)
and answers one question at each critical step: *what should happen now?* It escalates in four tiers
(deterministic rules → cheap typed decider → reasoning model → human), remembers lessons per automation,
replays a regression set every day, and never lets a model rewrite its own tests. Stdlib only. MIT.

*Module de décision bornée et d'amélioration continue, branchable sur n'importe quelle automatisation. Le reste de ce document est en français.*

```
événement ─▶ P0 règles (veto) ─▶ P1 décideur typé ─▶ P2 raisonneur (escalade) ─▶ P3 humain
                 ▲ leçons du namespace, jamais réécrites en bloc              │
                 └──────────── POST /outcome ◀── résultat réel ◀─────────────┘
```

## Pourquoi

Les agents IA branchés dans des automatisations « deviennent bêtes » avec le temps. Les causes sont mesurées :
le contexte se dégrade dès 32k tokens, les erreurs se composent et s'auto-renforcent, rien n'est retenu d'une
exécution à l'autre, le modèle change sans prévenir, et un agent autorisé à se modifier triche sur ses propres tests
dans 30 à 74 % des cas selon les études. Reason applique les correctifs qui marchent :

- **Décider peu, décider typé.** Le modèle ne peut émettre qu'une action d'un enum fermé (plus `abstain` et `ask_human`), avec des paramètres validés contre un schéma JSON. Un moteur de règles déterministe peut forcer une décision ou opposer un veto.
- **Raisonner seulement sur escalade.** Le raisonneur coûteux n'intervient que si le décideur doute, est interdit par une règle ou renvoie des paramètres invalides. Sinon on passe à l'humain.
- **Apprendre sans s'empoisonner.** Chaque échec ou correction humaine produit des propositions de leçons, mises en attente, approuvées par un humain, puis injectées dans les décisions suivantes avec compteurs utile/nuisible, déduplication et élagage. Jamais de réécriture globale du prompt.
- **Mesurer en continu.** Chaque `outcome` alimente un jeu de régression (pass@1, pass^k) et une empreinte déterministe de chaque modèle détecte les changements silencieux du fournisseur.
- **Garder la main.** Anti-boucle par `run_id`, idempotence, journal append-only, token bearer, et un agent de maintenance optionnel qui ne pousse jamais lui-même et dont les PR passent par un détecteur de falsification de tests.

## Installation

Aucune dépendance. Python 3.11 ou plus.

```bash
git clone https://github.com/4n0nn43x/reason && cd reason
cp examples/reason.config.json reason.config.json      # édite modèles et rôles
export OPENAI_API_KEY=... DEEPSEEK_API_KEY=... REASON_TOKEN=un-secret
python -m reason init acme/invoices                      # data/acme/invoices/{rules.json,policy.md}
python -m reason serve --port 8787
```

Optionnel : `uv pip install -e .` ou `pip install -e .` pour avoir la commande `reason`.

## Première décision

```bash
curl -s -X POST localhost:8787/decide -H "Authorization: Bearer $REASON_TOKEN" \
     -H 'Content-Type: application/json' -d @examples/request.example.json
```

Requête : `namespace` (une automatisation), `decision_point`, `event`, `state` (faits typés), `history`,
`candidate_actions` (chacune avec son schéma de paramètres et sa procédure de rollback), `constraints`, `idempotency_key`.

Réponse :

```json
{"decision_id": "d_3f9a…", "action": {"name": "escalate", "params": {}}, "tier": "decider", "confidence": 0.91,
 "rationale": "fournisseur inconnu, montant au-dessus du seuil appris L-007", "lessons_used": ["L-007"],
 "needs_human": {"required": false}, "rollback": null, "ttl_s": 600}
```

`tier` vaut `rules`, `decider`, `reasoner`, `loop`, `escalation` ou `human`. Quand `needs_human.required` est vrai,
la réponse contient la question, les options et un schéma plat prêt pour un formulaire, un hook Claude Code ou une élicitation MCP.

Quand ton automatisation a exécuté l'action, renvoie le résultat réel :

```bash
curl -s -X POST localhost:8787/outcome -H "Authorization: Bearer $REASON_TOKEN" -H 'Content-Type: application/json' \
     -d '{"decision_id":"d_3f9a…","result":"failure","human_feedback":{"correct_action":"ask_vendor","note":"IBAN inconnu"}}'
```

Un échec ou une correction déclenche le Reflector, qui propose 0 à 2 leçons courtes et testables :

```bash
python -m reason lessons acme/invoices pending
python -m reason lessons acme/invoices approve L-003     # ou reject
```

## Modèles et rôles : tout est modifiable

`reason.config.json` déclare des **modèles** et des **rôles** qui pointent vers eux. Un modèle est n'importe quelle API
chat completions compatible OpenAI (OpenAI, DeepSeek, OpenRouter, Together, LiteLLM, vLLM, Ollama) ou Jev.

```json
{"models": {"typed":    {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-6-luna", "json_schema": true},
            "reasoner": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-6.1-sol", "reasoning_effort": "high"},
            "deepseek": {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-flash", "json_schema": false, "reasoning_effort": "none"},
            "jev":      {"provider": "jev", "base_url": "https://api.typesafe.ai", "api_key_env": "TYPESAFE_API_KEY", "model": "jev-latest"}},
 "roles":  {"decider": "typed", "reasoner": "reasoner", "executor": "deepseek", "reflector": "typed"}}
```

| Rôle | Fait quoi | Si absent |
|---|---|---|
| `decider` | la décision typée (P1), schéma strict, `abstain` et `ask_human` toujours possibles | **l'`executor` décide lui-même** |
| `reasoner` | relance la décision à effort élevé quand P1 doute ou est interdit par une règle (P2) | on passe directement à l'humain |
| `executor` | `POST /execute` : rédaction, extraction, transformation avec les leçons du namespace (les « mains ») | obligatoire |
| `reflector` | transforme un échec en propositions de leçons | retombe sur `decider`, puis `executor` |

- Une requête peut surcharger les rôles : `"models": {"decider": "jev", "reasoner": "reasoner"}`.
- Un seul modèle qui décide et exécute : `examples/reason.config.executor-only.json`.
- **Jev** (TypeSafe AI) renvoie un choix calibré en moins d'une seconde pour 0,042 $ par million de tokens, directement ou via OpenRouter
  (`base_url` `https://openrouter.ai/api`). Il ne produit ni paramètres ni explication : une action qui exige des paramètres est escaladée au raisonneur.
- `json_schema: false` pour les fournisseurs sans sorties structurées strictes (DeepSeek) : Reason passe en `json_object` et valide côté code.

## Règles P0

`data/<namespace>/rules.json` :

```json
[{"id": "cap",   "if": [{"path": "state.facts.amount", "op": "gt", "value": 5000}], "forbid": ["approve"], "reason": "jamais d'approbation automatique au-dessus de 5000"},
 {"id": "small", "if": [{"path": "state.facts.vendor_known", "op": "eq", "value": true},
                        {"path": "state.facts.amount", "op": "lte", "value": 200}], "decide": {"name": "approve", "params": {"limit": 200}}}]
```

`decide` court-circuite les modèles (0 ms, 0 $). `forbid` oppose un **veto** à leur sortie : l'action interdite passe au palier suivant, puis à l'humain.
Opérateurs : `eq ne gt gte lt lte in nin contains exists regex`. La politique en prose, lue par les modèles, va dans `policy.md`.

## Façades : un moteur, trois branchements

| Façade | URL | Branchement |
|---|---|---|
| HTTP JSON | `POST /decide` `POST /outcome` `POST /execute` `GET /lessons/<ns>` `GET /health` | n8n HTTP Request, Make, Zapier, scripts, LangGraph, Temporal : c'est le **gate** universel. Workflow n8n prêt : `examples/n8n-workflow.json` |
| MCP | `POST /mcp` (JSON-RPC, stateless, outils `decide` `outcome` `execute`) | n8n MCP Client Tool, Claude Code, Claude Agent SDK, OpenAI Agents SDK, Cursor |
| Hook Claude Code | `POST /hooks/claude` | `PreToolUse` : Reason décide allow / deny / ask avant chaque outil ; `SessionStart` : injecte les leçons du projet. Config : `examples/claude-settings.json` |

Seul un hook, un middleware ou une étape explicite de workflow garantit la décision **avant** l'action. La façade MCP est un outil que l'agent hôte choisit d'appeler.

## Boucle d'évaluation

```bash
python -m reason eval acme/invoices -k 4     # rejoue data/<ns>/dataset.jsonl : pass@1 et pass^4
python -m reason fingerprint                 # empreinte déterministe de chaque modèle ; CHANGED si le fournisseur a modifié quelque chose
```

À planifier chaque jour (cron, n8n Schedule, GitHub Actions). Mesure `pass^k`, pas la moyenne : c'est le chiffre qui prédit la production.

## Agent de maintenance (optionnel)

`.github/workflows/reason-maintain.yml` lance une fois par semaine un agent qui lit les leçons récurrentes et les évals, puis propose
**un** patch sur `reason/` par pull request. Garde-fous : il ne pousse jamais lui-même, les tests et scripts sont relus depuis `main`,
le diff passe par `scripts/check_diff.py` (fichiers de tests touchés, `skip`/`xfail` ajoutés, assertions supprimées), l'auto-merge
n'est tenté qu'en dessous de 50 lignes et uniquement sous `reason/`, et un fichier `AGENT_PAUSED` à la racine suspend tout.

## Sécurité

- Si `REASON_TOKEN` est défini, `Authorization: Bearer` est obligatoire sur tout sauf `/health`. Lier le service à `127.0.0.1` ou le placer derrière un proxy.
- Corps limité à 1 Mo. Namespaces restreints à `org/automation/env` en caractères sûrs (pas de traversée de chemin).
- Les leçons ne sont jamais écrites directement par un modèle : file `pending`, approbation humaine.
- `state` et `event` sont des données non fiables : le prompt les traite comme telles, mais une injection reste possible. Les décisions irréversibles doivent rester derrière une règle P0 ou `ask_human`.

## Développement

```bash
python -m unittest discover -s tests -v      # 20 tests, aucun réseau
```

Arborescence : `reason/core.py` (moteur), `llm.py` (client), `jev.py` (adaptateur Jev), `lessons.py` (playbook), `reflector.py`,
`evalrun.py`, `server.py` (HTTP, MCP, hooks), `__main__.py` (CLI), `scripts/check_diff.py`, `examples/`.

## Feuille de route

- Façade `/v1/chat/completions` pour les hôtes qui ne savent parler qu'à un « modèle ».
- Promotion automatique des leçons quand l'eval reste verte, split de trafic canari par label.
- Nœud n8n vérifié et Agent Card A2A, comme clients fins de la même API.
- Adaptateur JEV-9B local (vLLM, logprobs) pour un décideur typé auto-hébergé.

## Références

Conçu à partir d'une revue de la littérature 2023-2026 : context rot (Chroma), « LLMs Get Lost in Multi-Turn Conversation »,
MAST, METR sur le reward hacking, ImpossibleBench, Darwin Gödel Machine, Agentic Context Engineering (ACE), GEPA, OWASP Top 10
for Agentic Applications, et les guides d'ingénierie d'Anthropic sur les agents longue durée.

Licence MIT.
