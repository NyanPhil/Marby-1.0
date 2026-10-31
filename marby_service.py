"""Operator-only closeout of a transaction after physically reconciling cash.

This does not dispense or refund cash. Never run before verifying the physical
coins actually released and documenting/refunding the customer's balance.
"""
import argparse
import json
import os
import time
from pathlib import Path
from coin_changer_hardware import atomic_json, project_path

arg = argparse.ArgumentParser()
arg.add_argument('--config', default=str(project_path('coin_changer_config.json')))
arg.add_argument('--operator', required=True)
arg.add_argument('--reason', required=True,
                 help='Description of inspection, delivered coins and refund/settlement')
a = arg.parse_args()
config_path = Path(a.config).expanduser().resolve()
config = json.loads(config_path.read_text())
p = Path(config.get('journal_file', 'transaction_state.json')).expanduser()
if not p.is_absolute():
    p = config_path.parent / p
if not p.exists():
    raise SystemExit('No transaction record found.')
state = json.loads(p.read_text())
if state.get('status') not in ('paid', 'dispensing', 'fault'):
    raise SystemExit('No unresolved transaction; no change made.')
print('CURRENT TRANSACTION:\n' + json.dumps(state, indent=2, ensure_ascii=False))
print('\nConfirm the customer has been made whole and inventory physically counted.')
if input('Enter RECONCILED to close this incident: ').strip() != 'RECONCILED':
    raise SystemExit('Aborted: state unchanged.')
resolution = {'operator': a.operator, 'reason': a.reason, 'time': time.time(),
              'previous_status': state['status'], 'transaction': state}
log = p.parent / 'reconciliations.jsonl'
with open(log, 'a', encoding='utf8') as f:
    f.write(json.dumps(resolution, ensure_ascii=False) + '\n')
    f.flush()
    os.fsync(f.fileno())
state['status'] = 'reconciled'
state['reconciliation'] = {'operator': a.operator, 'reason': a.reason, 'time': time.time()}
atomic_json(p, state)
print('Recorded operator resolution. Update inventory.json to match physically counted coins.')
