import type { DownloadMethod, YtdlpStatusResponse } from '../api/types';

export type DirectDownloadMethod = Exclude<DownloadMethod, 'slskd'>;

export const DIRECT_DOWNLOAD_METHODS: Array<{
  method: DirectDownloadMethod;
  shortLabel: string;
  label: string;
  title: string;
}> = [
  {
    method: 'spotiflac',
    shortLabel: 'SP',
    label: 'SpotiFLAC',
    title: 'Download with SpotiFLAC',
  },
  {
    method: 'ytdlp',
    shortLabel: 'YT',
    label: 'YouTube',
    title: 'Download with YouTube through yt-dlp',
  },
  {
    method: 'soundcloud',
    shortLabel: 'SC',
    label: 'SoundCloud',
    title: 'Download with SoundCloud through yt-dlp',
  },
];

export function downloadMethodLabel(method: DownloadMethod) {
  if (method === 'slskd') return 'SLSKD';
  return DIRECT_DOWNLOAD_METHODS.find((item) => item.method === method)?.label ?? method;
}

export function downloadMethodShortLabel(method: DownloadMethod) {
  if (method === 'slskd') return 'SLSKD';
  return DIRECT_DOWNLOAD_METHODS.find((item) => item.method === method)?.shortLabel ?? method;
}

export function directDownloadMethodEnabled(method: DirectDownloadMethod, status?: YtdlpStatusResponse | null) {
  if (!status?.ready) return false;
  if (method === 'spotiflac') return Boolean(status.spotiflac?.enabled ?? true);
  if (method === 'ytdlp') return Boolean(status.enabled && ((status.youtube as { ready?: boolean } | undefined)?.ready ?? true));
  return true;
}

export function anyDirectDownloadMethodEnabled(status?: YtdlpStatusResponse | null) {
  return DIRECT_DOWNLOAD_METHODS.some((source) => directDownloadMethodEnabled(source.method, status));
}

export function directDownloadMethodTitle(
  method: DirectDownloadMethod,
  status: YtdlpStatusResponse | null | undefined,
  fallbackMessage: string,
) {
  const source = DIRECT_DOWNLOAD_METHODS.find((item) => item.method === method);
  if (directDownloadMethodEnabled(method, status)) return source?.title ?? fallbackMessage;
  if (!status?.ready) return 'yt-dlp is still installing';
  if (method === 'spotiflac' && status.spotiflac && !status.spotiflac.enabled) {
    return 'SpotiFLAC is not available';
  }
  if (method === 'ytdlp' && status.youtube && !((status.youtube as { ready?: boolean }).ready ?? true)) {
    return 'YouTube source is degraded';
  }
  return fallbackMessage;
}
