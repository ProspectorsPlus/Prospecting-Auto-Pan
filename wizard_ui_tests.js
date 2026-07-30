// wizard_ui_tests.js -- DOM-level regression suite for the setup wizard.
// Driven by wizard_ui_tests.py, which renders the REAL embedded UI
// (build_html) plus canned bridge payloads composed by the REAL
// lite_onboarding.compose_registry, then runs this file under node with
// jsdom. Guards the behaviors the rc.3 wizard shipped without:
//   - guided calibration stays INSIDE the wizard (no Calibrate-tab switch)
//   - success returns to the checklist and activates the next step
//   - failure and cancel stay on the guided detail page
//   - stale/foreign overlay results cannot navigate
//   - sequential progression on permissions + calibration (ACTIVE /
//     UPCOMING labels, fading, completion activates the next step)
//   - the permission page carries no global never-requested list
//   - the main tutorial auto-starts once, only after setup finishes
// Prints "WIZARD-UI: ALL PASS" on success; exits 1 on any failure.
'use strict';
const fs = require('fs');
const path = require('path');
const WORK = process.argv[2];
if (!WORK) { console.error('usage: node wizard_ui_tests.js <workdir>'); process.exit(2); }
const ROOT = process.cwd();
const { JSDOM, VirtualConsole } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const html = fs.readFileSync(path.join(WORK, 'page.html'), 'utf8');
const REG_FRESH = JSON.parse(fs.readFileSync(path.join(WORK, 'reg_fresh.json'), 'utf8'));
const REG_CAP_DONE = JSON.parse(fs.readFileSync(path.join(WORK, 'reg_cap_done.json'), 'utf8'));
const REG_REVIEW = JSON.parse(fs.readFileSync(path.join(WORK, 'reg_review.json'), 'utf8'));
const TOURS = JSON.parse(fs.readFileSync(path.join(WORK, 'tours.json'), 'utf8'));

let failures = 0;
function chk(cond, msg) {
  if (cond) { console.log('  ok: ' + msg); }
  else { failures++; console.log('  FAIL: ' + msg); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---- canned bridge -----------------------------------------------------
function makeState() {
  return {
    calls: [],
    registry: REG_FRESH,
    granted: {},           // capId -> true
    onbState: 'NOT_STARTED',
    setupFinished: false,
    tutMain: 'NOT_STARTED',
    tutMarks: [],
  };
}
const mkCap = (id, title, level) => ({
  id, title, required_level: level, platforms: ['mac', 'win'],
  short_description: 's', data_accessed: 'd', data_retained: 'k',
  network_behaviour: 'no', operating_system_label: { mac: 'l', win: 'l' },
  detailed_explanation: 'e', declined_behaviour: 'c',
  revoke_instructions: { mac: 'r', win: 'r' }, privacy_notes: 'p',
});
function makeApi(S) {
  return new Proxy({}, { get(_, name) {
    if (name === 'then') return undefined;
    return (...args) => {
      S.calls.push(name + '(' + JSON.stringify(args).slice(1, -1) + ')');
      switch (name) {
        case 'welcome_state':
          return Promise.resolve({ show: true, show_every_launch: true,
            setup_needed: !S.setupFinished, resume: S.onbState,
            info: { version: 'test', name: 'Prospector Lite' } });
        case 'trust_state': {
          const caps = [
            mkCap('screen_detection', 'Screen detection', 'REQUIRED_FOR_CORE'),
            mkCap('input_control', 'Keyboard and mouse control', 'REQUIRED_FOR_CORE'),
            mkCap('stop_hotkeys', 'Safe Stop hotkeys', 'REQUIRED_FOR_CORE'),
            mkCap('discord_notifications', 'Discord notifications', 'OPTIONAL'),
            mkCap('coach_ai', 'Coach AI', 'OPTIONAL'),
            mkCap('sound_alerts', 'Sound alerts', 'INFORMATIONAL_ONLY'),
            mkCap('microphone', 'Microphone', 'NOT_REQUIRED'),
            mkCap('camera', 'Camera', 'NOT_REQUIRED'),
            mkCap('location', 'Location', 'NOT_REQUIRED'),
            mkCap('admin_privileges', 'Administrator privileges', 'NOT_REQUIRED'),
            mkCap('full_disk_access', 'Full disk access', 'NOT_REQUIRED'),
          ];
          caps.forEach(c => { c.live = {
            status: S.granted[c.id] ? 'granted' : 'not_granted',
            requested: false, requires_restart: false, test: null }; });
          return Promise.resolve({ platform: 'mac', capabilities: caps,
            seq: ++S._seq || (S._seq = 1), checked_at: 1, identity: {} });
        }
        case 'calibration_registry':
          return Promise.resolve(JSON.parse(JSON.stringify(S.registry)));
        case 'calibration_example':
          return Promise.resolve({ placeholder: true, alt: '' });
        case 'cue_mask_status':
          return Promise.resolve({ advanced: true, masks_only: false, cues: {
            PAN: { has: false }, DEPOSIT: { has: false }, SHAKE: { has: false } } });
        case 'readiness_check':
          return Promise.resolve({ ok: false, when: 1, items: [
            { id: 'calibration', title: 'Required calibration', status: 'fail',
              detail: 'Required items need attention: cue_masks', fix: 'calibration' },
            { id: 'cue_masks', title: 'Advanced cue matching', status: 'fail',
              detail: 'Required: capture the PAN, DEPOSIT, SHAKE prompt masks.',
              fix: 'calibration' }] });
        case 'detect_roblox':
          return Promise.resolve({ found: true, w: 1800, h: 1087, x: 0, y: 39 });
        case 'wizard_propose':
          // a real call does a full-screen grab + detection -- keep latency
          return new Promise(r => setTimeout(() =>
            r({ ok: true, detected: true, msg: 'found' }), 250));
        case 'onboarding_mark':
          S.onbState = args[0];
          if (args[0] === 'FINISHED') S.setupFinished = true;
          return Promise.resolve({ state: S.onbState });
        case 'tutorial_state':
          return Promise.resolve({ schema: 2, main: S.tutMain,
            setup_finished: S.setupFinished });
        case 'tutorial_mark':
          S.tutMarks.push(args[0] + (args[1] ? ':legacy' : ''));
          S.tutMain = args[0];
          return Promise.resolve({ ok: true, main: args[0] });
        case 'tutorial_content':
          return Promise.resolve(TOURS);
        case 'builds_info':
          return Promise.resolve([]);
        case 'studio_list':
          return Promise.resolve({ ok: true, scripts: [], active: '', meta: {} });
        case 'get_state':
          return Promise.resolve({ pixels: {}, colors: {}, fr: {},
            region_previews: {}, defaults: {}, v1: {}, v2: {}, geode: {},
            autobuild: {}, values: {}, running: false });
        default:
          return Promise.resolve({ ok: true, list: [], items: [], files: [],
            tours: {}, help: {}, cues: {}, scripts: {}, meta: {},
            entries: [], runs: [], relics: [], active: '', values: {} });
      }
    };
  } });
}

function boot(S) {
  const vc = new VirtualConsole();
  const errors = [];
  vc.on('jsdomError', e => errors.push(String((e && e.message) || e)));
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'https://localhost/', virtualConsole: vc,
    beforeParse(window) {
      window.scrollTo = () => {};
      if (!window.HTMLElement.prototype.scrollIntoView)
        window.HTMLElement.prototype.scrollIntoView = () => {};
    },
  });
  dom.errors = errors;
  return dom;
}
async function bridge(dom, S) {
  await sleep(30);
  dom.window.pywebview = { api: makeApi(S) };
  dom.window.dispatchEvent(new dom.window.Event('pywebviewready'));
  await sleep(1100); // boot() waits 650ms before deciding what to show
}
// Replicate the REAL bridge completion order: overlay_confirm /
// overlay_cancel always fire __calRefresh BEFORE __calDone
// (prospecting_app.py overlay_confirm -> _cal_done_notify). A suite that
// fires __calDone alone cannot see refresh-driven re-renders yanking the
// guided detail page -- exactly the rc.4 P0 the verifier found.
function calDone(dom, payload) {
  if (dom.window.__calRefresh) dom.window.__calRefresh();
  dom.window.__calDone(payload);
}
function view(doc) {
  const t = doc.querySelector('.tab.active');
  return {
    tab: t ? t.dataset.tab : '',
    setup: doc.getElementById('setup').classList.contains('show'),
    gate: doc.getElementById('gate').classList.contains('show'),
    supReturn: doc.getElementById('supReturn').classList.contains('show'),
    calModal: doc.getElementById('wizard') ? doc.getElementById('wizard').style.display : '',
    tour: doc.getElementById('tour') ? doc.getElementById('tour').style.display : '',
  };
}
function insideWizard(doc, label) {
  const v = view(doc);
  chk(v.tab !== 'cal', label + ': normal Calibration tab is NOT selected');
  chk(v.setup, label + ': setup wizard overlay stays visible');
  chk(v.calModal !== 'flex', label + ': legacy Calibrate-tab modal stays closed');
}
function cardState(doc, id) {
  const c = doc.querySelector('.cap-card[data-calid="' + id + '"]');
  if (!c) return null;
  const chip = c.querySelector('.step-chip');
  return {
    chip: chip ? chip.textContent.trim() : '',
    upcoming: c.classList.contains('step-upcoming'),
    active: c.classList.contains('step-active'),
  };
}

async function scenarioMainJourney() {
  console.log('[A] fresh first-run journey stays inside the wizard');
  const S = makeState();
  const dom = boot(S);
  const doc = dom.window.document;
  await bridge(dom, S);
  chk(view(doc).gate, 'welcome gate shows on first run');
  doc.getElementById('welGo').click();
  await sleep(300);
  chk(view(doc).setup && !view(doc).gate, 'Continue opens the setup wizard');

  // ---- permissions progression ----
  const body = doc.getElementById('supBody');
  chk(/Trust\s*&(amp;)?\s*Permissions/.test(body.innerHTML), 'trust step rendered');
  chk(!/Never requested \(so you can see we know\)/.test(body.innerHTML),
    'global never-requested list is GONE from the permission page');
  chk(/Trust Center/.test(body.textContent), 'concise Trust Center pointer replaces it');
  const sd = doc.querySelector('.cap-card[data-capid="screen_detection"]');
  const ic = doc.querySelector('.cap-card[data-capid="input_control"]');
  const sh = doc.querySelector('.cap-card[data-capid="stop_hotkeys"]');
  chk(sd && sd.classList.contains('step-active') && /Do this next/.test(sd.textContent),
    'first required permission is ACTIVE ("Do this next")');
  chk(ic && ic.classList.contains('step-upcoming') && /Upcoming/.test(ic.textContent),
    'second required permission is faded + labelled Upcoming');
  chk(sh && sh.classList.contains('step-upcoming') && /Upcoming/.test(sh.textContent),
    'third required permission is faded + labelled Upcoming');
  chk(!doc.querySelector('.cap-card[data-capid="microphone"]'),
    'never-requested capabilities are not rendered as wizard cards');

  // completion activates the next permission (fresh page entry after grant)
  S.granted.screen_detection = true;
  doc.querySelector('#supRail li[data-step="trust"]');
  // re-enter the page through the public surface (rail click not wired; use refresh path)
  doc.getElementById('trustRefresh').click();
  await sleep(400);
  const sd2 = doc.querySelector('.cap-card[data-capid="screen_detection"]');
  const ic2 = doc.querySelector('.cap-card[data-capid="input_control"]');
  chk(sd2 && /Complete/.test(sd2.textContent) && !sd2.classList.contains('step-upcoming'),
    'granted permission flips to COMPLETE');
  chk(ic2 && ic2.classList.contains('step-active') && /Do this next/.test(ic2.textContent),
    'completion activates the NEXT permission');

  // ---- calibration checklist ----
  doc.getElementById('supNext').click();
  await sleep(500);
  chk(/Guided Calibration/.test(body.innerHTML), 'calibration step rendered');
  chk(!body.querySelector('button[data-cal="wizard"]') && !body.querySelector('button[data-cal="tab"]'),
    'no Calibrate-tab escape buttons exist on the checklist');
  let st = cardState(doc, 'cap_bar');
  chk(st && st.active && st.chip === 'Do this next', 'Capacity is the first ACTIVE step');
  ['pan_prompt', 'deposit_prompt', 'shake_prompt', 'cue_masks'].forEach(id => {
    const s = cardState(doc, id);
    chk(s && s.upcoming && s.chip === 'Upcoming', id + ' is faded + labelled Upcoming');
  });
  chk((cardState(doc, 'dig_green') || {}).chip === 'Optional', 'optional items are labelled Optional');
  const cueCard = doc.querySelector('.cap-card[data-calid="cue_masks"]');
  chk(cueCard && /Required/.test(cueCard.textContent), 'Advanced cue matching is labelled Required');
  const upBtn = doc.querySelector('.cap-card[data-calid="pan_prompt"] button[data-gd]');
  chk(!upBtn, 'an Upcoming step has no enabled open button');

  // ---- capacity detail page ----
  const openBtn = doc.querySelector('button[data-gd="cap_bar"]');
  chk(!!openBtn, 'Capacity has an open button');
  openBtn.click();
  await sleep(400);
  chk(/Step 1/.test(body.textContent) && /Pan capacity bar/.test(body.textContent),
    'Capacity detail page opened inside the wizard');
  chk(/RIGHT tip/.test(body.textContent), 'Capacity instructions are specific (RIGHT tip)');
  chk(/Set up Roblox/i.test(body.textContent), 'detail page carries Roblox setup steps');
  insideWizard(doc, 'detail open');

  doc.getElementById('gdstart').click();
  await sleep(300);
  chk(S.calls.some(c => c.startsWith('wizard_propose("CAP_RIGHT"') && c.indexOf('guided_setup') >= 0),
    'Start runs the shared service (wizard_propose CAP_RIGHT, guided_setup context)');
  insideWizard(doc, 'capture 1 armed');
  calDone(dom, { ctx: 'guided_setup', key: 'CAP_RIGHT', ok: true });
  await sleep(900); // survive the 600ms __calRefresh window mid-plan
  chk(!!doc.getElementById('gdstart') || S.calls.some(c => c.startsWith('wizard_propose("CAP_LEFT"')),
    'detail flow survives the deferred checklist refresh after capture 1');
  await sleep(300);
  chk(S.calls.some(c => c.startsWith('wizard_propose("CAP_LEFT"')),
    'first confirm advances to the LEFT-tip capture');
  S.registry = REG_CAP_DONE; // the save happened; live status now user-calibrated
  calDone(dom, { ctx: 'guided_setup', key: 'CAP_LEFT', ok: true });
  await sleep(500);
  chk(/Saved and validated/.test(body.textContent), 'success state shows after validation');
  insideWizard(doc, 'validated');
  await sleep(1500); // auto-return to the checklist
  chk(/Guided Calibration/.test(body.innerHTML), 'success returns to the checklist automatically');
  st = cardState(doc, 'cap_bar');
  chk(st && st.chip === 'Complete', 'Capacity is now COMPLETE');
  const pan = cardState(doc, 'pan_prompt');
  chk(pan && pan.active && pan.chip === 'Do this next', 'the next step (Pan prompt) became ACTIVE');
  chk(!!doc.querySelector('button[data-gd="cap_bar"]'), 'completed steps stay reopenable');
  insideWizard(doc, 'back on checklist');

  // ---- cancel stays on the detail page ----
  doc.querySelector('button[data-gd="pan_prompt"]').click();
  await sleep(300);
  doc.getElementById('gdstart').click();
  await sleep(250);
  calDone(dom, { ctx: 'guided_setup', key: 'PAN_PIX', ok: false, cancelled: true });
  await sleep(250);
  chk(/Cancelled - nothing was saved/.test(body.textContent), 'cancel shows inside the detail page');
  chk(!!doc.getElementById('gdback'), 'cancel stays on the guided detail page');
  insideWizard(doc, 'after cancel');

  // ---- failure stays on the detail page ----
  doc.getElementById('gdstart').click();
  await sleep(250);
  calDone(dom, { ctx: 'guided_setup', key: 'PAN_PIX', ok: false });
  await sleep(250);
  chk(/did not save/.test(body.textContent), 'failure shows inside the detail page');
  chk(!!doc.getElementById('gdback'), 'failure stays on the guided detail page');
  await sleep(800); // the failure state must SURVIVE the deferred refresh
  chk(/did not save/.test(body.textContent) && !!doc.getElementById('gdback'),
    'failure state survives the 600ms __calRefresh window (stays on the page)');

  // ---- stale/foreign results cannot navigate ----
  doc.getElementById('gdstart').click();
  await sleep(250);
  dom.window.__calDone({ ctx: 'normal_calibration', key: 'PAN_PIX', ok: true }); // foreign ctx: deliberately no refresh pairing
  await sleep(250);
  chk(!!doc.getElementById('gdback'), 'a normal-tab result does not touch the guided page');
  doc.getElementById('gdback').click();
  await sleep(300);
  const beforeStray = body.innerHTML;
  calDone(dom, { ctx: 'guided_setup', key: 'PAN_PIX', ok: true });
  await sleep(400);
  chk(/Guided Calibration/.test(body.innerHTML) && body.innerHTML === beforeStray,
    'a stale result after leaving the page cannot navigate');

  // ---- readiness Fix Now deep-links inside the wizard ----
  doc.getElementById('supNext').click();
  await sleep(500);
  chk(/Readiness Check/.test(body.innerHTML), 'readiness page rendered');
  chk(/Advanced cue matching/.test(body.textContent), 'readiness shows the Advanced cue matching row');
  const fixBtns = body.querySelectorAll('button[data-fix="calibration"]');
  let cueFix = null;
  fixBtns.forEach(b => { if (b.dataset.fixitem === 'cue_masks') cueFix = b; });
  chk(!!cueFix, 'the cue-masks failure has a Fix now button');
  if (cueFix) { cueFix.click(); await sleep(400); }
  chk(/Advanced cue matching/.test(body.textContent) && !!doc.getElementById('gdstart'),
    'Fix now opens the guided cue detail page');
  insideWizard(doc, 'fix-now');

  // ---- tutorial does NOT start during setup, DOES start once after ----
  chk(view(doc).tour !== 'block', 'tutorial has not started during setup');
  doc.getElementById('gdback').click();
  await sleep(300);
  doc.getElementById('supNext').click(); // cal -> ready
  await sleep(500);
  doc.getElementById('supNext').click(); // ready -> finish
  await sleep(2200);
  chk(S.onbState === 'FINISHED', 'finishing marks onboarding FINISHED');
  chk(view(doc).tour === 'block', 'the main tutorial auto-starts after setup finishes');
  chk(S.tutMarks.indexOf('ACTIVE') >= 0, 'the start is persisted Python-side (ACTIVE)');
  // finish the tour via skip and confirm dismissal persists
  doc.getElementById('tourskip').click();
  await sleep(200);
  chk(S.tutMarks.indexOf('DISMISSED') >= 0, 'skipping records DISMISSED');
  // reopening the welcome now offers post-setup actions
  dom.window.openWelcome();
  await sleep(200);
  chk(doc.getElementById('welActions').style.display !== 'none',
    'post-setup welcome offers Review setup / Start tutorial / Trust Center');
  chk(dom.errors.length === 0, 'no jsdom errors during the journey' +
    (dom.errors.length ? ' :: ' + dom.errors[0] : ''));
  dom.window.close();
}

async function scenarioNoRestart() {
  console.log('[B] finished install: tutorial does not restart; needs-review shown');
  const S = makeState();
  S.setupFinished = true;
  S.onbState = 'FINISHED';
  S.tutMain = 'DISMISSED';
  S.registry = REG_REVIEW;
  const dom = boot(S);
  const doc = dom.window.document;
  await bridge(dom, S);
  chk(view(doc).gate, 'welcome still shows (preference ON)');
  doc.getElementById('welGo').click();
  await sleep(2200);
  chk(view(doc).tour !== 'block', 'a DISMISSED tutorial does not restart on launch');
  chk(S.tutMarks.length === 0, 'no tutorial transition is written');
  // the wizard reopened from Trust Center shows the needs-review position
  dom.window.SETUP.open('cal');
  await sleep(500);
  const cue = cardState(doc, 'cue_masks');
  chk(cue && cue.chip === 'Needs review' && cue.active,
    'single-pixel-only install shows Advanced cue matching as NEEDS REVIEW at its position');
  ['cap_bar', 'pan_prompt', 'deposit_prompt', 'shake_prompt'].forEach(id => {
    const s = cardState(doc, id);
    chk(s && s.chip === 'Complete', id + ' stays COMPLETE (old values preserved)');
  });
  dom.window.close();
}

async function scenarioLegacyTourFlag() {
  console.log('[C] legacy localStorage flag migrates instead of restarting');
  const S = makeState();
  S.setupFinished = true;
  S.onbState = 'FINISHED';
  const dom = boot(S);
  const doc = dom.window.document;
  try { dom.window.localStorage.setItem('pp_tour_done', '1'); } catch (e) {}
  await bridge(dom, S);
  doc.getElementById('welGo').click();
  await sleep(2200);
  chk(view(doc).tour !== 'block', 'legacy users are not forced through the tour again');
  chk(S.tutMarks.indexOf('COMPLETED:legacy') >= 0, 'the legacy flag migrates as COMPLETED');
  dom.window.close();
}

(async () => {
  await scenarioMainJourney();
  await scenarioNoRestart();
  await scenarioLegacyTourFlag();
  if (failures) { console.log('WIZARD-UI: %d FAILURE(S)', failures); process.exit(1); }
  console.log('WIZARD-UI: ALL PASS');
  process.exit(0);
})().catch(e => { console.error('WIZARD-UI: DRIVER ERROR', e); process.exit(1); });
