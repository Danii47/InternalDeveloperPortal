// Editor de interfaces de red (NICs) reutilizable por el provisionamiento y por el editor
// deploy_vms de Blueprints. Cada NIC: { bridge, ip_mode: 'dhcp'|'static', ip, gateway }.
// El control del bridge se inyecta por callback (concreto en provisión; concreto o {{var}} en
// blueprints), y opcionalmente se decoran los inputs ip/gateway (p.ej. selector {{var}}).

export interface Nic { bridge: string; ip_mode: 'dhcp' | 'static'; ip?: string; gateway?: string; }

const FIELD = 'w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:ring-2 focus:ring-blue-500 outline-none';

export interface NicEditorOpts {
  nics: Nic[];                                                   // se muta en el sitio
  renderBridge: (nic: Nic, refresh: () => void) => HTMLElement;  // control de bridge según contexto
  decorateText?: (input: HTMLInputElement, key: 'ip' | 'gateway', nic: Nic) => void;
  onChange?: () => void;
}

export function nicEditor(opts: NicEditorOpts): HTMLElement {
  const root = document.createElement('div');
  root.className = 'space-y-2';
  const list = document.createElement('div');
  list.className = 'space-y-2';
  root.appendChild(list);

  const changed = () => opts.onChange && opts.onChange();
  const refresh = () => { render(); changed(); };

  function textField(label: string, ph: string, val: string, onInput: (v: string) => void,
                     key: 'ip' | 'gateway', nic: Nic): HTMLElement {
    const w = document.createElement('div');
    w.innerHTML = `<label class="block text-[11px] text-slate-500 mb-0.5">${label}</label>`;
    const inp = document.createElement('input');
    inp.type = 'text'; inp.placeholder = ph; inp.value = val || ''; inp.className = FIELD;
    inp.addEventListener('input', () => onInput(inp.value));
    w.appendChild(inp);
    if (opts.decorateText) opts.decorateText(inp, key, nic);
    return w;
  }

  function render() {
    list.innerHTML = '';
    if (!opts.nics.length) {
      const empty = document.createElement('div');
      empty.className = 'text-[11px] text-slate-600 py-1';
      empty.textContent = 'Sin interfaces de red. Añade la primera ↓';
      list.appendChild(empty);
    }
    opts.nics.forEach((nic, idx) => {
      if (nic.ip_mode !== 'static') nic.ip_mode = 'dhcp';
      const row = document.createElement('div');
      row.className = 'bg-slate-950/50 border border-slate-800 rounded-lg p-2.5 space-y-2 ui-fade-in';

      const head = document.createElement('div');
      head.className = 'flex items-center justify-between';
      head.innerHTML = `<span class="text-[11px] font-semibold text-slate-300">🔌 Interfaz ${idx}</span>`;
      const rm = document.createElement('button');
      rm.type = 'button'; rm.textContent = '✕'; rm.title = 'Quitar interfaz';
      rm.className = 'w-5 h-5 rounded bg-slate-800 hover:bg-red-600 text-slate-300 text-[10px] transition-colors';
      rm.addEventListener('click', () => { opts.nics.splice(idx, 1); refresh(); });
      head.appendChild(rm);
      row.appendChild(head);

      const bridgeWrap = document.createElement('div');
      bridgeWrap.innerHTML = '<label class="block text-[11px] text-slate-500 mb-0.5">Bridge / VNet</label>';
      bridgeWrap.appendChild(opts.renderBridge(nic, refresh));
      row.appendChild(bridgeWrap);

      const modeWrap = document.createElement('div');
      modeWrap.className = 'flex gap-2';
      (['dhcp', 'static'] as const).forEach((m) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = m === 'dhcp' ? 'DHCP' : 'IP estática';
        b.className = 'flex-1 text-xs py-1.5 rounded-lg border transition-colors ' +
          (nic.ip_mode === m
            ? 'bg-blue-600 border-blue-600 text-white'
            : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-600');
        b.addEventListener('click', () => { nic.ip_mode = m; refresh(); });
        modeWrap.appendChild(b);
      });
      row.appendChild(modeWrap);

      if (nic.ip_mode === 'static') {
        const grid = document.createElement('div');
        grid.className = 'grid grid-cols-2 gap-2';
        grid.appendChild(textField('IP (CIDR)', '10.0.40.10/24', nic.ip || '', (v) => { nic.ip = v; changed(); }, 'ip', nic));
        grid.appendChild(textField('Gateway (opcional)', '10.0.40.1', nic.gateway || '', (v) => { nic.gateway = v; changed(); }, 'gateway', nic));
        row.appendChild(grid);
      }
      list.appendChild(row);
    });
  }

  const add = document.createElement('button');
  add.type = 'button';
  add.textContent = '➕ Añadir interfaz';
  add.className = 'text-xs text-blue-400 hover:text-blue-300 transition-colors';
  add.addEventListener('click', () => { opts.nics.push({ bridge: '', ip_mode: 'dhcp' }); refresh(); });
  root.appendChild(add);

  render();
  return root;
}

/** Migra los params legacy (bridges/ip_mode/ip_start/gateway) a una lista de NICs. */
export function nicsFromLegacy(p: any): Nic[] {
  let bridges = p.bridges;
  if (typeof bridges === 'string') bridges = bridges.split(',').map((x: string) => x.trim()).filter(Boolean);
  if (!Array.isArray(bridges)) bridges = bridges ? [bridges] : [];
  const isStatic = String(p.ip_mode || 'dhcp').toLowerCase() === 'static';
  return bridges.map((b: string, idx: number) =>
    idx === 0 && isStatic
      ? { bridge: b, ip_mode: 'static' as const, ip: p.ip_start || '', gateway: p.gateway || '' }
      : { bridge: b, ip_mode: 'dhcp' as const });
}
