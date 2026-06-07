#!/usr/bin/env python3
"""Render the swarm audit JSON into a clean AUDIT.md deliverable."""
import json

revs = json.load(open('/tmp/zd_audit.json'))
SEV = {'critical': '🔴 CRITICAL', 'high': '🟠 HIGH', 'medium': '🟡 MEDIUM', 'low': '⚪ LOW'}
order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}

allf = []
for r in revs:
    for f in r.get('findings', []):
        f['_dim'] = r['dimension']
        allf.append(f)
from collections import Counter
counts = Counter(f['severity'] for f in allf)

DIM_LABEL = {
    'architecture': 'Architecture & Duplication',
    'performance': 'Performance',
    'scalability_maintainability': 'Scalability & Maintainability',
    'security_correctness': 'Security & Correctness',
    'Frontend Security, UX, and Code Quality Audit': 'Frontend',
}

L = []
w = L.append
w('# ZeroDTE — Senior Engineering Audit')
w('')
w('_Reverse-engineered and audited by a 5-reviewer swarm (architecture · performance · '
  'scalability/maintainability · security/correctness · frontend), each reading the real code._')
w('')
w(f"**{len(allf)} findings** — "
  f"{counts.get('critical',0)} critical · {counts.get('high',0)} high · "
  f"{counts.get('medium',0)} medium · {counts.get('low',0)} low.")
w('')
w('> Scope note: this is a **single-operator paper-trading system**, not a web-scale product. '
  'Severities are calibrated to *that* reality — real correctness/race/security risks that bite a '
  'solo operator, not theoretical multi-tenant concerns.')
w('')

# ---- Critical problem areas ----
w('## 🔴 Critical problem areas (fix first)')
w('')
crit = [f for f in allf if f['severity'] == 'critical']
for f in crit:
    w(f"### {f['title']}")
    w(f"**Where:** `{f['location']}`  ·  _{DIM_LABEL.get(f['_dim'], f['_dim'])}_")
    w('')
    w(f"**Problem.** {f['problem']}")
    w('')
    w(f"**Impact.** {f['impact']}")
    w('')
    w(f"**Fix.** {f['fix']}")
    w('')

# ---- Full findings by dimension ----
w('## Full findings by dimension')
w('')
for r in revs:
    w(f"### {DIM_LABEL.get(r['dimension'], r['dimension'])}")
    w('')
    w(f"_{r['summary']}_")
    w('')
    fs = sorted(r['findings'], key=lambda f: order.get(f['severity'], 9))
    for f in fs:
        w(f"#### {SEV.get(f['severity'], f['severity'])} — {f['title']}")
        w(f"- **Where:** `{f['location']}`")
        w(f"- **Problem:** {f['problem']}")
        w(f"- **Impact:** {f['impact']}")
        w(f"- **Fix:** {f['fix']}")
        w('')

open('/Users/xynkro/Documents/Trading/ZeroDTE/docs/AUDIT.md', 'w').write('\n'.join(L))
print('wrote docs/AUDIT.md —', len(allf), 'findings,', len('\n'.join(L)), 'chars')
