#!/usr/bin/env node
// feature-spec — 기능 설계 명세(의도 → 다이어그램 → 착수) 문서를 만들고 검증하는 도구.
// 문서 1개 = 기능 1개. docs/design/NNNN-slug.md 에 산다.
// Node 18+ 내장 모듈만 사용(외부 의존성 없음).

import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync } from 'node:fs';
import { join, resolve, dirname, basename } from 'node:path';

// ── 섹션 레지스트리 ────────────────────────────────────────────────
// tiers: 이 섹션이 필수인 tier들. mermaid: 그 섹션에서 요구하는 다이어그램 종류.
const SECTIONS = [
  {
    n: 1, key: 'intent', title: '의도 (Intent)', tiers: 'SML',
    body: [
      '| 항목 | 내용 |',
      '|---|---|',
      '| 문제 | {{지금 무엇이 아픈가. 증상이 아니라 아픈 사람 기준으로}} |',
      '| 왜 지금 | {{안 하면 무엇이 막히나}} |',
      '| 주 사용자 | {{1명. 그의 실제 작업 흐름}} |',
      '| 불변식 | {{절대 깨지 않을 것. 예: 원본 불변, 비밀은 프로세스를 안 떠남}} |',
      '| 비목표 | {{이번에 명시적으로 안 하는 것}} |',
    ],
  },
  {
    n: 2, key: 'acceptance', title: '수용 기준 (Acceptance)', tiers: 'SML',
    body: [
      '| ID | 수용 기준 | 검증 방법 |',
      '|---|---|---|',
      '| AC-1 | {{관측 가능한 문장. "빠르다" 금지, "p95 < 200ms" 로}} | {{테스트 파일 / 수동 절차 / 렌더 육안}} |',
      '| AC-2 |  |  |',
    ],
  },
  {
    n: 3, key: 'findings', title: '확정 사실 (Findings)', tiers: 'SML',
    body: [
      '설계 전에 **실제로 확인한 것만** 적는다. 추측은 §10 으로 보낸다.',
      '',
      '| 항목 | 확인된 사실 | 설계 반영 |',
      '|---|---|---|',
      '| {{의존성/버전/기존 동작}} | {{어떻게 확인했는지까지}} | {{그래서 설계가 어떻게 달라지나}} |',
    ],
  },
  {
    n: 4, key: 'usecase', title: '유스케이스 / 시나리오', tiers: 'ML', mermaid: 'flowchart',
    body: [
      '```mermaid',
      'flowchart LR',
      '  U["사용자"] --> UC1["유스케이스 1"]',
      '  UC1 -. include .-> UC2["공통 절차"]',
      '  UC1 --> EXT["외부 시스템"]',
      '```',
      '',
      '**주 시나리오**: 사용자가 …하면 → …가 되고 → …로 끝난다',
      '',
      '**예외 시나리오**: 실패 시 무엇이 남고 무엇이 되돌아가나',
    ],
  },
  {
    n: 5, key: 'pipeline', title: '파이프라인 (flowchart)', tiers: 'ML', mermaid: 'flowchart',
    body: [
      '```mermaid',
      'flowchart TD',
      '  A["진입점"] --> B["처리"]',
      '  B --> C{"분기 조건"}',
      '  C -->|성공| D["결과"]',
      '  C -->|실패| E["에러 처리"]',
      '```',
    ],
  },
  {
    n: 6, key: 'state', title: '액션 · 상태 전이 (action diagram)', tiers: 'ML', mermaid: 'stateDiagram',
    body: [
      '```mermaid',
      'stateDiagram-v2',
      '  [*] --> Idle',
      '  Idle --> Working: 트리거',
      '  Working --> Done: 성공',
      '  Working --> Failed: 실패',
      '  Failed --> Idle: 재시도',
      '```',
      '',
      '| 상태 | 저장/이벤트 | UI 반응 |',
      '|---|---|---|',
      '| Working |  |  |',
      '| Failed | {{잔여물 정리까지 적는다}} |  |',
    ],
  },
  {
    n: 7, key: 'timing', title: '타이밍 (sequence)', tiers: 'L', mermaid: 'sequenceDiagram',
    body: [
      '동시성·지연·타임아웃이 설계를 바꾸는 경우에만 채운다.',
      '',
      '```mermaid',
      'sequenceDiagram',
      '  participant U as 사용자',
      '  participant BE as Backend',
      '  participant EX as 외부',
      '  U->>BE: 요청',
      '  BE->>EX: 위임',
      '  Note over EX: 최장 구간 — 실측값을 적는다',
      '  EX-->>BE: 응답',
      '  BE-->>U: 완료',
      '  Note over U,BE: 총 소요 → timeout 값 근거',
      '```',
    ],
  },
  {
    n: 8, key: 'data', title: '데이터 (ER)', tiers: 'L', mermaid: 'erDiagram',
    body: [
      '스키마가 바뀌면 채운다. 마이그레이션·CASCADE/SET NULL 을 반드시 표기.',
      '',
      '```mermaid',
      'erDiagram',
      '  PARENT ||--o{ CHILD : has',
      '  PARENT {',
      '    int id PK',
      '  }',
      '  CHILD {',
      '    int id PK',
      '    int parent_id FK',
      '  }',
      '```',
    ],
  },
  {
    n: 9, key: 'changes', title: '변경 지점', tiers: 'SML',
    body: [
      '경로는 백틱으로. 새로 만드는 파일은 `(신규)` 를 붙인다 — `check` 가 실존 여부를 본다.',
      '',
      '| 파일 | 변경 |',
      '|---|---|',
      '| `path/to/file.py` | {{무엇을 어떻게}} |',
      '| `path/to/new.py` (신규) | {{역할}} |',
    ],
  },
  {
    n: 10, key: 'risks', title: '잠재 문제 & 대응', tiers: 'SML',
    body: [
      '| 문제 | 대응 |',
      '|---|---|',
      '| {{아직 확인 못한 가정}} | {{틀렸을 때의 폴백}} |',
    ],
  },
  {
    n: 11, key: 'plan', title: '착수 순서', tiers: 'SML',
    body: [
      '각 항목에 담당 AC 를 적는다. `check` 가 고아 AC 를 잡아낸다.',
      '',
      '- [ ] 1. 얇은 수직 슬라이스 — 버튼 하나가 실제로 되는 단위 (AC-1)',
      '- [ ] 2. 다음 슬라이스 (AC-2)',
    ],
  },
  {
    n: 12, key: 'trace', title: '추적성', tiers: 'L',
    body: [
      '규제 대응(SDD/RMF) 문서로 나갈 때만. 구현이 끝나면 채운다.',
      '',
      '| AC | 구현 커밋 | 테스트 | 위험 항목 |',
      '|---|---|---|---|',
      '| AC-1 |  |  |  |',
    ],
  },
];

const TIER_NAMES = {
  S: '작음 — 의도·수용기준·확정사실·변경지점·착수순서',
  M: '표준 — + 유스케이스·파이프라인·상태전이',
  L: '전체 — + 타이밍·ER·추적성 (규제/외부 인터페이스/동시성)',
};
const STATUSES = ['draft', 'approved', 'building', 'done'];

// ── 유틸 ──────────────────────────────────────────────────────────
function parseArgs(argv) {
  const a = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const t = argv[i];
    if (t.startsWith('--')) {
      const k = t.slice(2);
      const v = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
      a[k] = v;
    } else a._.push(t);
  }
  return a;
}

function opt(args, k) {
  return args[k] && args[k] !== true ? String(args[k]) : '';
}

function repoRoot(start) {
  try {
    return execFileSync('git', ['-C', start, 'rev-parse', '--show-toplevel'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return resolve(start);
  }
}

function specDir(root) { return join(root, 'docs', 'design'); }

function slugify(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'feature';
}

function today() { return new Date().toISOString().slice(0, 10); }

function nextId(dir) {
  if (!existsSync(dir)) return 1;
  let max = 0;
  for (const f of readdirSync(dir)) {
    const m = f.match(/^(\d{4})-/);
    if (m) max = Math.max(max, +m[1]);
  }
  return max + 1;
}

function readSpec(file) {
  const raw = readFileSync(file, 'utf8');
  const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  const fm = {};
  if (m) {
    for (const line of m[1].split(/\r?\n/)) {
      const kv = line.match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/);
      if (kv) fm[kv[1]] = kv[2].trim().replace(/^["']|["']$/g, '');
    }
  }
  return { raw, fm, body: m ? raw.slice(m[0].length) : raw };
}

// 본문을 "## n. 제목" 단위로 쪼갠다.
function splitSections(body) {
  const out = [];
  const re = /^##\s+(\d+)\.\s+(.+?)\s*$/gm;
  const hits = [...body.matchAll(re)];
  for (let i = 0; i < hits.length; i++) {
    const start = hits[i].index + hits[i][0].length;
    const end = i + 1 < hits.length ? hits[i + 1].index : body.length;
    out.push({ n: +hits[i][1], title: hits[i][2], text: body.slice(start, end) });
  }
  return out;
}

function mermaidBlocks(text) {
  return [...text.matchAll(/```mermaid\r?\n([\s\S]*?)```/g)].map((m) => m[1]);
}

function resolveSpecPath(root, arg) {
  const dir = specDir(root);
  if (!arg) {
    const files = existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.md')).sort() : [];
    if (files.length !== 1) {
      throw new Error(files.length === 0
        ? `${dir} 에 명세가 없다. 먼저 'new' 로 만들 것.`
        : `명세가 ${files.length}개다. 파일을 지정할 것:\n  ${files.join('\n  ')}`);
    }
    return join(dir, files[0]);
  }
  if (existsSync(arg)) return resolve(arg);
  const cand = join(dir, arg.endsWith('.md') ? arg : `${arg}.md`);
  if (existsSync(cand)) return cand;
  if (existsSync(dir)) {
    const hit = readdirSync(dir).find((f) => f.includes(arg) && f.endsWith('.md'));
    if (hit) return join(dir, hit);
  }
  throw new Error(`명세 파일을 못 찾음: ${arg}`);
}

// ── new ───────────────────────────────────────────────────────────
function cmdNew(root, args) {
  const title = opt(args, 'title') || args._[1];
  if (!title) throw new Error('제목이 필요하다: spec.mjs new "제목" [--tier M] [--issue 12]');
  const tier = (opt(args, 'tier') || 'M').toUpperCase();
  if (!TIER_NAMES[tier]) throw new Error(`tier 는 S|M|L 중 하나: ${tier}`);

  const dir = specDir(root);
  mkdirSync(dir, { recursive: true });
  const id = String(nextId(dir)).padStart(4, '0');
  const slug = slugify(opt(args, 'slug') || title);
  const file = join(dir, `${id}-${slug}.md`);
  if (existsSync(file)) throw new Error(`이미 있다: ${file}`);

  const L = [
    '---',
    `id: ${id}-${slug}`,
    `title: ${title}`,
    `tier: ${tier}`,
    'status: draft',
    `issue: ${opt(args, 'issue')}`,
    `owner: ${opt(args, 'owner') || process.env.USERNAME || process.env.USER || ''}`,
    `created: ${today()}`,
    '---',
    '',
    `# ${title}`,
    '',
    `> tier **${tier}** — ${TIER_NAMES[tier]}`,
    '> 채우는 순서: §1 의도 → §2 수용기준 → §3 확정사실(조사) → 다이어그램 → §9 변경지점 → §11 착수순서',
    '',
  ];
  for (const s of SECTIONS) {
    const required = s.tiers.includes(tier);
    L.push(`## ${s.n}. ${s.title}${required ? '' : ' *(선택 — 이 tier 에선 생략 가능)*'}`, '');
    if (required) L.push(...s.body, '');
    else L.push('<!-- 이 tier 에선 필수가 아니다. 필요 없으면 섹션째로 지워라. -->', '');
  }
  writeFileSync(file, L.join('\n'), 'utf8');
  console.log(`생성: ${file}`);
  console.log(`tier ${tier} — 필수 섹션 §${SECTIONS.filter((s) => s.tiers.includes(tier)).map((s) => s.n).join(', §')}`);
  return 0;
}

// ── check ─────────────────────────────────────────────────────────
function cmdCheck(root, args) {
  const file = resolveSpecPath(root, args._[1]);
  const { fm, body } = readSpec(file);
  const errs = [];
  const warns = [];

  const tier = String(fm.tier || '').toUpperCase();
  if (!TIER_NAMES[tier]) errs.push(`frontmatter tier 가 S|M|L 이 아님: "${fm.tier ?? ''}"`);
  if (!fm.title) errs.push('frontmatter title 없음');
  if (!STATUSES.includes(fm.status)) errs.push(`frontmatter status 는 ${STATUSES.join('|')} 중 하나여야 함: "${fm.status ?? ''}"`);

  const secs = splitSections(body);
  const byN = new Map(secs.map((s) => [s.n, s]));
  const need = SECTIONS.filter((s) => !TIER_NAMES[tier] || s.tiers.includes(tier));

  for (const s of need) {
    const got = byN.get(s.n);
    if (!got) { errs.push(`§${s.n} "${s.title}" 섹션이 없다`); continue; }
    const meat = got.text.replace(/<!--[\s\S]*?-->/g, '').trim();
    if (!meat) errs.push(`§${s.n} "${s.title}" 이 비어 있다`);
    if (s.mermaid) {
      const blocks = mermaidBlocks(got.text);
      if (!blocks.length) errs.push(`§${s.n} 에 mermaid 다이어그램이 없다 (${s.mermaid} 필요)`);
      else if (!blocks.some((b) => b.trim().startsWith(s.mermaid))) {
        errs.push(`§${s.n} 의 다이어그램이 ${s.mermaid} 가 아니다`);
      }
    }
  }

  // 미치환 플레이스홀더 / 미결 표기
  const ph = (body.match(/\{\{[^}\n]*\}\}/g) || []);
  if (ph.length) warns.push(`미치환 플레이스홀더 {{...}} ${ph.length}건`);
  const tbd = (body.match(/\b(TBD|TODO|FIXME)\b/g) || []).length;
  if (tbd) warns.push(`TBD/TODO ${tbd}건 잔존`);

  // 수용 기준
  const acSec = byN.get(2);
  const acs = [];
  if (acSec) {
    for (const line of acSec.text.split(/\r?\n/)) {
      const m = line.match(/^\|\s*(AC-\d+)\s*\|(.*)$/);
      if (!m) continue;
      const cols = m[2].split('|').map((c) => c.trim());
      acs.push({ id: m[1], text: cols[0] || '', verify: cols[1] || '' });
    }
    if (!acs.length) errs.push('§2 에 AC-n 행이 하나도 없다');
    const seen = new Set();
    for (const ac of acs) {
      if (seen.has(ac.id)) errs.push(`§2 ${ac.id} 가 중복`);
      seen.add(ac.id);
      if (!ac.text) errs.push(`§2 ${ac.id} 의 기준 문구가 비었다`);
      if (!ac.verify) errs.push(`§2 ${ac.id} 의 검증 방법이 비었다 — 검증 못 하는 기준은 기준이 아니다`);
    }
  }

  // 착수 순서 / AC 커버리지
  const planSec = byN.get(11);
  const tasks = planSec ? [...planSec.text.matchAll(/^\s*-\s*\[([ xX])\]\s*(.+)$/gm)] : [];
  if (planSec && !tasks.length) errs.push('§11 에 체크박스 항목(`- [ ]`)이 없다');
  const planText = planSec ? planSec.text : '';
  for (const ac of acs) {
    if (!planText.includes(ac.id)) warns.push(`${ac.id} 를 §11 착수 순서에서 아무도 안 맡았다`);
  }

  // 변경 지점 경로 실존
  const chSec = byN.get(9);
  if (chSec) {
    let rows = 0;
    for (const line of chSec.text.split(/\r?\n/)) {
      const t = line.trim();
      if (!t.startsWith('|') || /^\|[\s:-]+\|/.test(t)) continue;
      const first = line.split('|')[1];
      if (!first) continue;
      const p = (first.match(/`([^`]+)`/) || [])[1];
      if (!p || !/[/.]/.test(p)) continue;
      rows++;
      const isNew = /신규|new/i.test(line);
      const abs = join(root, p);
      if (!isNew && !existsSync(abs)) errs.push(`§9 경로가 실재하지 않는다: ${p} — 신규면 (신규) 를 붙일 것`);
      // 이미 구현 중/완료면 신규 파일이 존재하는 게 정상 — draft·approved 단계에서만 경고한다.
      if (isNew && existsSync(abs) && (fm.status === 'draft' || fm.status === 'approved')) {
        warns.push(`§9 ${p} 가 (신규)로 적혔는데 이미 있다`);
      }
    }
    if (!rows) warns.push('§9 에 백틱 경로가 하나도 없다');
  }

  // status 정합성
  const doneCnt = tasks.filter((t) => t[1].toLowerCase() === 'x').length;
  if (fm.status === 'done' && doneCnt < tasks.length) {
    errs.push(`status=done 인데 §11 미완료 항목이 ${tasks.length - doneCnt}개 남았다`);
  }
  if (fm.status === 'draft' && doneCnt > 0) warns.push('status=draft 인데 완료된 항목이 있다 — building 으로 올릴 것');

  console.log(`${basename(file)}  [tier ${tier || '?'} · ${fm.status || '?'}]`);
  console.log(`  수용기준 ${acs.length}개 · 착수항목 ${doneCnt}/${tasks.length}`);
  for (const w of warns) console.log(`  ! ${w}`);
  for (const e of errs) console.log(`  x ${e}`);
  if (!errs.length && !warns.length) console.log('  ok — 이상 없음');
  return errs.length ? 1 : 0;
}

// ── status ────────────────────────────────────────────────────────
function cmdStatus(root) {
  const dir = specDir(root);
  if (!existsSync(dir)) { console.log(`${dir} 없음`); return 0; }
  const files = readdirSync(dir).filter((f) => f.endsWith('.md')).sort();
  if (!files.length) { console.log('명세 없음'); return 0; }
  console.log(`${'명세'.padEnd(41)} tier  status     진행      AC`);
  for (const f of files) {
    const { fm, body } = readSpec(join(dir, f));
    const secs = splitSections(body);
    const plan = secs.find((s) => s.n === 11);
    const tasks = plan ? [...plan.text.matchAll(/^\s*-\s*\[([ xX])\]/gm)] : [];
    const done = tasks.filter((t) => t[1].toLowerCase() === 'x').length;
    const ac = secs.find((s) => s.n === 2);
    const acN = ac ? (ac.text.match(/^\|\s*AC-\d+\s*\|/gm) || []).length : 0;
    const bar = tasks.length ? `${done}/${tasks.length}` : '-';
    console.log(`${f.slice(0, 40).padEnd(41)} ${String(fm.tier || '?').padEnd(5)} ${String(fm.status || '?').padEnd(10)} ${bar.padEnd(9)} ${acN}`);
  }
  return 0;
}

// ── render ────────────────────────────────────────────────────────
// 다이어그램을 뽑아 육안 컨펌용으로 만든다. mermaid-cli(mmdc)가 있으면 PNG, 없으면 .mmd.
function cmdRender(root, args) {
  const file = resolveSpecPath(root, args._[1]);
  const { body } = readSpec(file);
  const outDir = opt(args, 'out') ? resolve(opt(args, 'out'))
    : join(dirname(file), 'render', basename(file, '.md'));
  mkdirSync(outDir, { recursive: true });

  let mmdc = null;
  for (const c of ['mmdc', 'mmdc.cmd']) {
    try { execFileSync(c, ['--version'], { stdio: 'ignore' }); mmdc = c; break; } catch { /* 없음 */ }
  }

  let n = 0;
  for (const s of splitSections(body)) {
    const blocks = mermaidBlocks(s.text);
    blocks.forEach((b, i) => {
      const base = `${String(s.n).padStart(2, '0')}-${slugify(s.title)}${blocks.length > 1 ? `-${i + 1}` : ''}`;
      const mmd = join(outDir, `${base}.mmd`);
      writeFileSync(mmd, b, 'utf8');
      n++;
      if (mmdc) {
        try {
          execFileSync(mmdc, ['-i', mmd, '-o', join(outDir, `${base}.png`), '-b', 'white'], { stdio: 'ignore' });
        } catch {
          console.log(`  ! ${base}: mmdc 실패 — .mmd 만 남김`);
        }
      }
    });
  }
  console.log(`${n}개 다이어그램 → ${outDir}`);
  if (!mmdc) console.log('  mmdc 없음 → .mmd 만 생성. PNG 가 필요하면: npm i -g @mermaid-js/mermaid-cli');
  return 0;
}

// ── main ──────────────────────────────────────────────────────────
const HELP = `feature-spec — 기능 설계 명세 도구

  node spec.mjs new "<제목>" [--tier S|M|L] [--issue N] [--slug s] [--owner o]
  node spec.mjs check [<명세>]                  명세 정합성 검사 (실패 시 exit 1)
  node spec.mjs status                          docs/design 전체 진행 현황
  node spec.mjs render [<명세>] [--out DIR]     다이어그램 -> .mmd/.png (육안 컨펌용)

공통: --repo <경로>   기본은 현재 디렉터리의 git 루트
<명세>는 파일 경로 · id · 부분 문자열 아무거나. 명세가 1개뿐이면 생략 가능.`;

function main() {
  const args = parseArgs(process.argv.slice(2));
  const cmd = args._[0];
  const root = repoRoot(opt(args, 'repo') || process.cwd());
  const table = { new: cmdNew, check: cmdCheck, status: cmdStatus, render: cmdRender };
  if (!cmd || !table[cmd] || args.help) {
    console.log(HELP);
    return cmd && !table[cmd] ? 1 : 0;
  }
  return table[cmd](root, args);
}

try {
  process.exit(main());
} catch (e) {
  console.error(`오류: ${e.message}`);
  process.exit(1);
}
