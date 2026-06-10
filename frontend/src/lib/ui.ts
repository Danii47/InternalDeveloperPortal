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

/**
 * Modal de confirmación con la estética de la app. Crea un <dialog> al vuelo y devuelve
 * una promesa que resuelve true (confirmar) o false (cancelar / cerrar con Esc).
 */
export function confirmDialog(
  title: string,
  message: string,
  opts: { okLabel?: string; cancelLabel?: string; danger?: boolean } = {},
): Promise<boolean> {
  const { okLabel = 'Eliminar', cancelLabel = 'Cancelar', danger = true } = opts;
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.className = 'm-auto bg-slate-900 border border-slate-800 rounded-xl p-0 w-full max-w-md text-slate-300 shadow-2xl backdrop:bg-black/60';
    const body = document.createElement('div');
    body.className = 'p-5';
    body.innerHTML =
      `<h3 class="text-lg font-semibold text-white mb-1.5"></h3>` +
      `<p class="text-sm text-slate-400"></p>` +
      `<div class="flex gap-2 justify-end mt-5">` +
      `<button data-act="cancel" class="ui-btn ui-btn-ghost"></button>` +
      `<button data-act="ok" class="ui-btn ${danger ? 'ui-btn-danger' : 'ui-btn-primary'}"></button>` +
      `</div>`;
    (body.querySelector('h3') as HTMLElement).textContent = title;
    (body.querySelector('p') as HTMLElement).textContent = message;
    (body.querySelector('[data-act=cancel]') as HTMLElement).textContent = cancelLabel;
    (body.querySelector('[data-act=ok]') as HTMLElement).textContent = okLabel;
    dlg.appendChild(body);
    document.body.appendChild(dlg);

    let settled = false;
    const done = (val: boolean) => {
      if (settled) return;
      settled = true;
      try { dlg.close(); } catch (_) {}
      dlg.remove();
      resolve(val);
    };
    body.querySelector('[data-act=ok]')!.addEventListener('click', () => done(true));
    body.querySelector('[data-act=cancel]')!.addEventListener('click', () => done(false));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); done(false); });
    dlg.showModal();
  });
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

/**
 * Envuelve un <input type=number> con botones − / + a juego (oculta las flechas nativas).
 * Mantiene el MISMO nodo input (sus listeners siguen vivos) y dispara 'input'/'change' al pulsar.
 */
export function numberStepper(input: HTMLInputElement | null): void {
  if (!input || input.dataset.stepped) return;
  input.dataset.stepped = '1';
  const parent = input.parentElement;
  const anchor = input.nextSibling;
  const wrap = document.createElement('div');
  wrap.className = 'ui-stepper';
  const mk = (txt: string, fn: () => void) => {
    const b = document.createElement('button');
    b.type = 'button'; b.tabIndex = -1; b.className = 'ui-stepper-btn'; b.textContent = txt;
    b.addEventListener('click', () => {
      if (input.disabled) return;
      fn();
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    return b;
  };
  wrap.appendChild(mk('−', () => input.stepDown()));
  wrap.appendChild(input);
  wrap.appendChild(mk('+', () => input.stepUp()));
  if (parent) parent.insertBefore(wrap, anchor);
}

// — Validadores cliente ligeros (el backend es la autoridad) —
export function looksLikeIp(v: string): boolean {
  const s = (v || '').trim();
  return /^(\d{1,3})(\.\d{1,3}){3}$/.test(s) && s.split('.').every((o) => +o <= 255);
}
export function looksLikeStaticCidr(v: string): boolean {
  const m = (v || '').trim().match(/^(\d{1,3}(?:\.\d{1,3}){3})\/(\d{1,2})$/);
  if (!m) return false;
  return m[1].split('.').every((o) => +o <= 255) && +m[2] >= 1 && +m[2] <= 31; // máscara real (no /32)
}
export function looksLikeCidr(v: string): boolean {
  const m = (v || '').trim().match(/^(\d{1,3}(?:\.\d{1,3}){3})\/(\d{1,2})$/);
  if (!m) return false;
  return m[1].split('.').every((o) => +o <= 255) && +m[2] <= 32;
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
