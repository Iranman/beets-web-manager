import json, subprocess

repo_dir = 'C:/Users/irand/repos/beets-web-manager'
res = subprocess.run(['git', 'show', 'b9a96bdfa6b9bf478dd111a8eb038d77cee5be38:security/arch003_mutation_inventory.json'], capture_output=True, text=True, cwd=repo_dir)
data = json.loads(res.stdout)
inventory = data['inventory']

sinks = [e for e in inventory if e.get('domain') == 'import_reconciliation' and e.get('classification') in ('ARCH003_BLOCKER', 'NEEDS_REVIEW', 'ENGINE_GENERIC_BYPASS')]

print(f"Total import_reconciliation starting sinks: {len(sinks)}")
for i, s in enumerate(sinks, 1):
    print(f"{i:2d}. key={s['key']}\n    file={s['file']} | function={s['function']} | line={s['line']} | kind={s['kind']}\n    call_text={s['call_text']}")
