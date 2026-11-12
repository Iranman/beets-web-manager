import { formatEpochDateTime } from '../../lib/dates';
import { errorMessage, panelError } from '../../lib/errors';
import { useEngineOffline } from '../../components/EngineOfflineBanner';
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useState } from 'react';
import { getConfigFile, revertConfigFile, saveConfigFile } from '../../api/client';
import type { ConfigFileResponse } from '../../api/types';

// Settings > config.yaml: the Beets config editor, moved unchanged from the System page.
// Save and Revert still go through the confirmation dialog and the loaded revision.

type ConfigAction = 'save' | 'revert' | null;

function formatBackupTime(value?: number | null): string {
  if (!value) return 'No backup';
  return `Backup from ${formatEpochDateTime(value)}`;
}

function ConfigActionDialog({
  action,
  onClose,
  onConfirm,
  busy,
}: {
  action: ConfigAction;
  onClose: () => void;
  onConfirm: () => void;
  busy: boolean;
}) {
  const isSave = action === 'save';
  return (
    <Dialog open={action !== null} onClose={busy ? () => undefined : onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/60" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-sm rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">
            {isSave ? 'Save config.yaml?' : 'Revert config.yaml?'}
          </DialogTitle>
          <p className="mt-2 text-sm text-zinc-400">
            {isSave
              ? 'The current file will be backed up before the new content is written.'
              : 'The backup file will replace the current config.yaml.'}
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outlined" size="small" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="contained"
              size="small"
              color={isSave ? 'primary' : 'warning'}
              onClick={onConfirm}
              disabled={busy}
            >
              {busy ? 'Working...' : isSave ? 'Save' : 'Revert'}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

export function ConfigYamlEditor() {
  const engineOffline = useEngineOffline();
  const [configText, setConfigText] = useState('');
  const [savedConfigText, setSavedConfigText] = useState('');
  const [configMeta, setConfigMeta] = useState<Pick<ConfigFileResponse, 'revision' | 'has_backup' | 'backup_ts'> | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [configBusy, setConfigBusy] = useState(false);
  const [configError, setConfigError] = useState('');
  const [configMsg, setConfigMsg] = useState('');
  const [configAction, setConfigAction] = useState<ConfigAction>(null);

  const loadConfig = useCallback(async () => {
    setConfigLoading(true);
    setConfigError('');
    try {
      const cfg = await getConfigFile();
      const content = cfg.content ?? '';
      setConfigText(content);
      setSavedConfigText(content);
      setConfigMeta({ revision: cfg.revision || '', has_backup: Boolean(cfg.has_backup), backup_ts: cfg.backup_ts ?? null });
    } catch (err) {
      setConfigError(errorMessage(err));
    } finally {
      setConfigLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  const configDirty = configText !== savedConfigText;
  const configEmpty = !configText.trim();

  const handleSaveConfig = async () => {
    setConfigBusy(true);
    setConfigMsg('');
    setConfigError('');
    try {
      if (!configMeta?.revision) throw new Error('Reload config.yaml before saving.');
      const saved = await saveConfigFile(configText, configMeta.revision);
      await loadConfig();
      setConfigMsg(saved.backed_up ? 'Saved config.yaml. Backup updated.' : 'Saved config.yaml.');
    } catch (err) {
      setConfigError(errorMessage(err));
    } finally {
      setConfigBusy(false);
      setConfigAction(null);
    }
  };

  const handleRevertConfig = async () => {
    setConfigBusy(true);
    setConfigMsg('');
    setConfigError('');
    try {
      if (!configMeta?.revision) throw new Error('Reload config.yaml before reverting.');
      await revertConfigFile(configMeta.revision);
      await loadConfig();
      setConfigMsg('Reverted config.yaml from backup.');
    } catch (err) {
      setConfigError(errorMessage(err));
    } finally {
      setConfigBusy(false);
      setConfigAction(null);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <code className="text-zinc-400">/config/config.yaml</code>
          <span>{formatBackupTime(configMeta?.backup_ts)}</span>
          {configDirty && <span className="font-semibold text-amber-400">Unsaved</span>}
        </div>
        <div className="flex gap-2">
          <Button size="small" variant="outlined" onClick={loadConfig} disabled={configLoading || configBusy}>Reload</Button>
          <Button size="small" variant="outlined" color="warning" onClick={() => setConfigAction('revert')} disabled={configLoading || configBusy || !configMeta?.has_backup || !configMeta?.revision}>Revert</Button>
          <Button size="small" variant="contained" onClick={() => setConfigAction('save')} disabled={configLoading || configBusy || !configDirty || configEmpty || !configMeta?.revision}>Save config.yaml</Button>
        </div>
      </div>
      {configMsg && <Alert severity="success" onClose={() => setConfigMsg('')}>{configMsg}</Alert>}
      {panelError(configError, engineOffline) && <Alert severity="error">{configError}</Alert>}
      {configLoading && <LinearProgress sx={{ borderRadius: 1 }} />}
      <TextField
        value={configText}
        onChange={(event) => setConfigText(event.target.value)}
        fullWidth
        multiline
        minRows={10}
        maxRows={20}
        disabled={configLoading || configBusy}
        spellCheck={false}
        slotProps={{
          htmlInput: { 'aria-label': 'config.yaml contents' },
          input: {
            sx: {
              alignItems: 'flex-start',
              bgcolor: 'var(--bw-bg)',
              color: 'var(--bw-text)',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
              fontSize: '0.78rem',
              lineHeight: 1.55,
              overflowY: 'auto',
            },
          },
        }}
      />

      <ConfigActionDialog
        action={configAction}
        onClose={() => setConfigAction(null)}
        onConfirm={configAction === 'save' ? handleSaveConfig : handleRevertConfig}
        busy={configBusy}
      />
    </div>
  );
}
