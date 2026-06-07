// Kit de UI imperativo compartido para las páginas que construyen DOM por JS
// (constructor de Blueprints, etc.). Para marcado estático usa components/ui/*.astro.

export type ToastKind = 'success' | 'error' | 'info';

/** Notificación efímera abajo-derecha. Se apila y se autodescarta. */
export function toast(msg: string, kind: ToastKind = 'info'): void {
  let box = document.getElementById('toastBox');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toastBox';
    box.className = 'fixed bottom-4 right-4 z-[100] flex flex-col gap-2 items-end';
    document.body.appendChild(box);
  }
  const tone: Record<ToastKind, string> = {
    success: 'border-emerald-500/40 text-emerald-200',
    error: 'border-red-500/40 text-red-200',
    info: 'border-blue-500/40 text-blue-200',
  };
  const t = document.createElement('div');
  t.className = `max-w-xs bg-slate-900 border ${tone[kind]} rounded-lg px-4 py-3 text-xs shadow-2xl transition-all duration-300 translate-y-2 opacity-0`;
  t.textContent = msg;
  box.appendChild(t);
  requestAnimationFrame(() => t.classList.remove('translate-y-2', 'opacity-0'));
  setTimeout(() => { t.classList.add('opacity-0'); setTimeout(() => t.remove(), 300); }, 5000);
}

// Estados de run/tarea/ciclo de vida → clase .ui-badge-*
const BADGE_MAP: Record<string, string> = {
  running: 'ui-badge-running', pending: 'ui-badge-idle',
  completed: 'ui-badge-ok', success: 'ui-badge-ok', active: 'ui-badge-ok',
  failed: 'ui-badge-error', error: 'ui-badge-error', rollback_partial: 'ui-badge-error',
  rolled_back: 'ui-badge-warn', expiring: 'ui-badge-warn',
};
export function statusBadgeClass(status: string): string {
  return 'ui-badge ' + (BADGE_MAP[status] || 'ui-badge-idle');
}
export function statusBadge(status: string, label?: string): string {
  return `<span class="${statusBadgeClass(status)}">${label ?? status}</span>`;
}

/** Icono "?" con tooltip accesible (hover/foco). El texto se inserta sin riesgo de inyección. */
export function helpIcon(text: string): HTMLElement {
  const wrap = document.createElement('span');
  wrap.className = 'relative inline-flex group align-middle ml-1';
  wrap.innerHTML =
    '<button type="button" tabindex="0" aria-label="Ayuda" class="w-3.5 h-3.5 inline-flex items-center justify-center rounded-full bg-slate-700 text-slate-300 text-[9px] font-bold leading-none hover:bg-blue-600 hover:text-white transition-colors cursor-help">?</button>' +
    '<span role="tooltip" class="pointer-events-none absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 w-56 px-2.5 py-1.5 rounded-md bg-slate-950 border border-slate-700 text-[10px] leading-snug text-slate-300 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity duration-150 z-50 shadow-xl"></span>';
  (wrap.querySelector('[role=tooltip]') as HTMLElement).textContent = text;
  return wrap;
}
