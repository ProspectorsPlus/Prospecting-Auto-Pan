"""
finds_current_code.py  — REFERENCE EXTRACT (read-only)

This is the CURRENT finds-detection subsystem from prospecting_old.py, pulled
out verbatim so you can study it without the 3,700-line engine around it. It
does NOT run standalone (references module globals: State, log, np, json,
time, threading, Vision/Foundation, and the FINDS_/SELL_/FIND_ config globals).
See the FINDS_DETECTION_REWORK_PROMPT.md for what to change and why.

Config globals it reads (defaults shown): FINDS_TRACK, FINDS_OCR_MS=250,
FINDS_STACK_NEWEST="bottom", FINDS_MIN_CONF=0.30, FINDS_EMPTY_CLEAR=3,
FINDS_BANK_RARITY="Exotic", SELL_BOOST_PCT, FIND_TL_PIXEL, FIND_BR_PIXEL.

Emit protocol (engine -> app stdout):
  __FIND__ {id,t,mod,name,kg,rarity,value,mmul}       (a new find)
  __FIND_UPD__ {same}                                 (refine same id; delta-exact)
"""

# ---- config constants (in the real engine these live near the top) ----------
# FINDS TRACKER (opt-in): OCR the bottom-right item pop-up ("Iridescent
# Dinosaur Skull  347 kg" / "Exotic") every FINDS_OCR_MS, log every find
# (modifier / name / weight / rarity, deduped), and estimate the value of
# KEPT items via prospecting_prices.json x SELL_BOOST_PCT -- auto-sold money
# is already measured by the money OCR; kept loot is what needs estimating.
FINDS_TRACK        = False
FINDS_OCR_MS       = 1200
FINDS_STACK_NEWEST = "bottom"  # where a new find card appears (bottom | top)
FINDS_MIN_CONF     = 0.30      # min OCR confidence to accept a NEW card (fade
                              # guard -- a faint ghost won't spawn a phantom)
FINDS_EMPTY_CLEAR  = 3         # empty frames before the stack is 'cleared'
SELL_BOOST_PCT     = 100    # your sell boost, in percent (100 = 1.0x)
FINDS_BANK_RARITY  = "Exotic"  # only value finds at/above this rarity as KEPT
                               # loot -- lower-rarity finds auto-sell and are
                               # already in the money counter (no double-count)
FIND_TL_PIXEL      = [0, 0] # find pop-up region corners (calibrate)


# ---- the finds subsystem (helpers + FindsWatcher) ---------------------------
_RARITIES = ("Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic",
             "Exotic", "Divine", "Relic")
_RARITY_RANK = {r: i for i, r in enumerate(_RARITIES)}


def _rarity_at_least(r, floor):
    """True if rarity r is at least `floor` in the ladder. Unknown r -> False."""
    a = _RARITY_RANK.get((r or "").title())
    b = _RARITY_RANK.get((floor or "").title(), 0)
    return a is not None and a >= b
# Value-tier modifiers from the v2.6 model (Section 4). These are the ones
# that carry a price multiplier; "modifier_mult" in the price table sets each.
_MODIFIERS = {"Shiny", "Pure", "Glowing", "Scorching", "Irradiated",
              "Iridescent", "Cosmic", "Mutated", "Lunar", "Perfect"}


def _load_prices():
    """prospecting_prices.json (next to the config): user-editable table for
    valuing KEPT finds. {"per_kg": {item: $/kg}, "rarity_mult": {...},
    "modifier_mult": {...}}. Missing file/keys -> value 0 (still logged)."""
    try:
        with open(os.path.join(os.path.dirname(CONFIG_FILE),
                               "prospecting_prices.json")) as f:
            return json.load(f)
    except Exception:
        return {}


class FindsWatcher:
    """FINDS TRACKER (see constants). Background OCR on the item pop-up:
    every find is parsed into (modifier, name, kg, rarity), deduped, logged
    as a __FIND__ line for the app's Analytics window, counted into the run
    stats, and valued via the price table x SELL_BOOST_PCT."""

    def __init__(self):
        self._stop = threading.Event()
        self._tracked = []     # live cards in the stack, TOP->BOTTOM (see below)
        self._next_id = 1      # monotonic find id
        self._empty = 0        # consecutive frames with no cards

    def start(self):
        if not FINDS_TRACK:
            return
        tl, br = FIND_TL_PIXEL, FIND_BR_PIXEL
        try:
            x0, y0, x1, y1 = int(tl[0]), int(tl[1]), int(br[0]), int(br[1])
        except Exception:
            return
        if x1 - x0 < 20 or y1 - y0 < 10:
            log("[finds] FINDS_TRACK is on but the pop-up region isn't "
                "calibrated (Calibrate tab -> Find pop-up corners)")
            return
        try:
            import Vision, Foundation  # noqa: F401
        except Exception:
            log("[finds] macOS Vision OCR not available -> finds tracking off")
            return
        self.prices = _load_prices()
        self.ore_names = list((self.prices.get("per_kg") or {}).keys())
        self.reg = {"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()
        log("[finds] finds tracker running (stack mode)")

    def stop(self):
        self._stop.set()

    def _run(self):
        import mss
        with mss.mss() as sct:
            while not self._stop.is_set() and (State.running or State.paused):
                try:
                    self._scan(sct)
                except Exception:
                    pass
                self._stop.wait(max(0.4, FINDS_OCR_MS / 1000.0))

    def _scan(self, sct):
        """STACK TRACKER. The finds HUD is a scrolling list: a new item slides
        in at one end (newest), pushing the others along, and every card fades
        out over a few seconds. That is a FEED, so we count it like one --
        by SEQUENCE, not by content or colour.

        Each frame we OCR the whole calibrated box into ordered cards (top ->
        bottom) and align them to the cards we were already tracking. Because
        new cards append at one end and old cards drop off the other, the
        current cards are (a suffix of what we had) + (any brand-new cards at
        the end). New cards => new finds -- and since it's positional, two
        IDENTICAL Mythical duplicates are two separate cards and both count.
        Matched cards accumulate more reads so the name/modifier/weight settle
        to their best value; a card is finalised (best value locked) when it
        leaves the stack."""
        cards = _cards_from_lines(_ocr_lines(sct, self.reg), self.ore_names)
        if not cards:
            self._empty += 1
            if self._empty >= max(2, FINDS_EMPTY_CLEAR):
                for tc in self._tracked:      # stack really cleared -> finalise
                    self._emit(tc, final=True)
                self._tracked = []
                self._empty = 0
            return
        self._empty = 0

        # normalise so the NEWEST card is at the END of the list (append side)
        newest_bottom = (FINDS_STACK_NEWEST or "bottom").lower() != "top"
        cur = cards if newest_bottom else list(reversed(cards))
        prev = self._tracked

        # find how many old cards dropped off the front (s), i.e. the shift that
        # best aligns prev[s:] with the front of cur
        best_s, best_score = 0, -1
        for s in range(len(prev) + 1):
            ov = min(len(prev) - s, len(cur))
            score = sum(1 for i in range(ov)
                        if _card_same(prev[s + i]["rep"], cur[i]))
            # reward more matches, then a longer clean overlap, then smaller s
            score = score * 100 + ov - s
            if score > best_score:
                best_score, best_s = score, s
        s = best_s
        ov = min(len(prev) - s, len(cur))

        # cards that dropped off the front -> finalise & remove
        for tc in prev[:s]:
            self._emit(tc, final=True)
        kept = prev[s:]

        new_tracked = []
        # matched (still-present) cards: fold in this frame's read
        for i in range(ov):
            tc = kept[i]
            self._add_read(tc, cur[i])
            new_tracked.append(tc)
        # brand-new cards at the end -> new finds
        for j in range(ov, len(cur)):
            c = cur[j]
            if c["conf"] < FINDS_MIN_CONF:     # too faint to trust as new
                continue
            tc = {"id": self._next_id, "reads": [], "emitted": None,
                  "seen": 0}
            self._next_id += 1
            self._add_read(tc, c)
            new_tracked.append(tc)
        # any kept cards beyond the overlap that cur didn't reach (partial read)
        for tc in kept[ov:]:
            new_tracked.append(tc)             # keep; not seen this frame

        self._tracked = new_tracked[-16:]      # cap
        # emit / update finds that have settled
        for tc in self._tracked:
            if tc["seen"] >= 2:
                self._emit(tc, final=False)

    def _add_read(self, tc, card):
        tc["reads"].append((card["name"], card["mod"], card["kg"],
                            card.get("conf", 1.0)))
        if len(tc["reads"]) > 30:
            tc["reads"] = tc["reads"][-30:]
        tc["seen"] += 1
        tc["rep"] = {"name": card["name"], "mod": card["mod"],
                     "kg": card["kg"]}       # frame-matching representative

    def _resolve(self, tc):
        """Best (name, mod, kg, rarity) from a card's accumulated reads:
        majority ore name, majority non-blank modifier, MODE weight."""
        from collections import Counter
        reads = tc["reads"]
        names = [n for n, _m, _k, _c in reads if n]
        if not names:
            return None
        name = Counter(names).most_common(1)[0][0]
        mods = [m for _n, m, _k, _c in reads if m]
        mod = Counter(mods).most_common(1)[0][0] if mods else ""
        kgs = [k for _n, _m, k, _c in reads if k > 0]
        if kgs:
            c = Counter(round(k, 1) for k in kgs)
            top = c.most_common()
            best_n = top[0][1]
            tied = sorted(v for v, n in top if n == best_n)
            kg = tied[len(tied) // 2]
        else:
            kg = 0.0
        rarity = (self.prices.get("rarity_of") or {}).get(name, "")
        return name, mod, kg, rarity

    def _emit(self, tc, final):
        """Emit (or update) one find. Counts once per card id; when its best
        value changes (more reads, or on departure) it re-states with the same
        id and the run stats are adjusted by the delta so totals stay exact."""
        r = self._resolve(tc)
        if r is None:
            return
        name, mod, kg, rarity = r
        value = self._value(name, mod, rarity, kg)
        prevrec = tc.get("emitted")
        if prevrec and (prevrec["name"], prevrec["mod"], round(prevrec["kg"], 1)) \
                == (name, mod, round(kg, 1)) and not final:
            return                             # nothing changed
        st = State.stats
        mmul = (self.prices.get("modifier_mult") or {}).get(mod, 1.0) if mod else 1.0
        rec = {"id": tc["id"],
               "t": round(State.stats.runtime(), 1) if State.stats else 0,
               "mod": mod, "name": name, "kg": kg, "rarity": rarity,
               "value": int(value), "mmul": float(mmul)}
        if prevrec is None:
            if st is not None:
                st.finds_count += 1
                st.find_kg += kg
                st.best_kg = max(st.best_kg, kg)
                st.by_rarity[rarity or "?"] = st.by_rarity.get(rarity or "?", 0) + 1
                st.by_mod[mod or "plain"] = st.by_mod.get(mod or "plain", 0) + 1
                st.loot_value += value
            print("__FIND__ " + json.dumps(rec), flush=True)
            log("[finds] %s%s %skg %s%s"
                % ((mod + " ") if mod else "", name, kg, rarity,
                   (" ~$%s" % f"{int(value):,}") if value else ""))
        else:
            if st is not None:                 # correct the totals by the delta
                st.find_kg += kg - prevrec["kg"]
                st.best_kg = max(st.best_kg, kg)
                st.loot_value += value - prevrec["value_f"]
                if (prevrec["rarity"] or "?") != (rarity or "?"):
                    st.by_rarity[prevrec["rarity"] or "?"] = max(
                        0, st.by_rarity.get(prevrec["rarity"] or "?", 1) - 1)
                    st.by_rarity[rarity or "?"] = st.by_rarity.get(rarity or "?", 0) + 1
                if (prevrec["mod"] or "plain") != (mod or "plain"):
                    st.by_mod[prevrec["mod"] or "plain"] = max(
                        0, st.by_mod.get(prevrec["mod"] or "plain", 1) - 1)
                    st.by_mod[mod or "plain"] = st.by_mod.get(mod or "plain", 0) + 1
            print("__FIND_UPD__ " + json.dumps(rec), flush=True)
        tc["emitted"] = {"name": name, "mod": mod, "kg": kg,
                         "rarity": rarity, "value_f": float(value)}

    def _value(self, name, mod, rarity, kg):
        """Estimated sale value of one KEPT find (v2.6 money model):
            per_kg * kg * modifier_mult * rarity_mult * (1 + SellBoost/100)
        Only finds at/above FINDS_BANK_RARITY are valued -- lower ones auto-sell
        and are already counted by the money reader, so valuing them here would
        double-count. per_kg for the Dinosaur Skull already folds in its coreB
        banking x2; skulls carry their own 7,110% sell boost via
        per_item_sell_boost."""
        p = self.prices or {}
        # bank rarity floor: below it -> auto-sold -> already in the money OCR
        eff_rarity = rarity or (p.get("rarity_of") or {}).get(name, "")
        if not _rarity_at_least(eff_rarity, FINDS_BANK_RARITY):
            return 0.0
        per = (p.get("per_kg") or {}).get(name, 0)
        if not per:
            return 0.0
        rmul = float((p.get("rarity_mult") or {}).get(eff_rarity, 1.0))
        mmul = float((p.get("modifier_mult") or {}).get(mod, 1.0)) if mod else 1.0
        bank = float((p.get("bank_mult") or {}).get(name, 1.0))
        sb = (p.get("per_item_sell_boost") or {}).get(name)
        sb = float(sb) if sb is not None else float(SELL_BOOST_PCT)
        return float(per) * kg * rmul * mmul * bank * (1.0 + sb / 100.0)


def _best_match(s, options, cutoff=0.6):
    """Nearest string in `options` by difflib ratio, or None below cutoff."""
    import difflib
    s = (s or "").strip().lower()
    if not s or not options:
        return None
    best, score = None, cutoff
    for o in options:
        r = difflib.SequenceMatcher(None, s, o.lower()).ratio()
        if r >= score:
            best, score = o, r
    return best


def _snap_name(raw, ore_names):
    """Snap an OCR'd item string to (canonical_name, modifier).
    Ores are 1-2 words; modifiers are a leading word. Tries the last two
    words and the last word against the ore list, and the first word against
    the modifier list -- so 'Irradlatod Divoscrur Shall' -> (Dinosaur Skull,
    Irradiated) even with heavy OCR noise."""
    words = (raw or "").split()
    if not words:
        return None, ""
    # ore name: best of last-2-words and last-1-word
    cands = []
    if len(words) >= 2:
        cands.append(" ".join(words[-2:]))
    cands.append(words[-1])
    if len(words) >= 3:
        cands.append(" ".join(words[-3:]))
    name = None
    for c in cands:
        m = _best_match(c, ore_names, cutoff=0.55)
        if m:
            name = m
            break
    if name is None:
        name = raw.strip().title()          # unknown ore: keep cleaned raw
    # modifier: first word vs the known tiers
    mod = ""
    if len(words) > 1:
        mm = _best_match(words[0], list(_MODIFIERS), cutoff=0.6)
        if mm:
            mod = mm
    return name, mod


_KG_RE = __import__("re").compile(r"(\d[\d,\. ]*)\s*k\s*g", __import__("re").I)


def _ocr_lines(sct, reg):
    """OCR the whole finds region with position + confidence per text line.

    Returns [{t, cy, conf, h}] where cy is the line's vertical centre as a
    TOP-DOWN normalised fraction (0 = top of region, 1 = bottom), conf is the
    recognition confidence (0..1 -> a fade/freshness proxy: a bright new card
    reads with high confidence, a faded one lower), h is the line height
    (normalised). Everything is LOCATION-based inside the calibrated box -- no
    dependence on the terrain colour behind the cards."""
    img = sct.grab(reg)
    import mss.tools
    arr = np.frombuffer(img.rgb, dtype=np.uint8).reshape(
        img.height, img.width, 3)
    big = arr.repeat(3, axis=0).repeat(3, axis=1)     # 3x upscale (readable)
    png = mss.tools.to_png(big.tobytes(), (img.width * 3, img.height * 3))
    from Foundation import NSData
    import Vision
    data = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    try:
        req.setRecognitionLevel_(0)          # accurate (small HUD text)
        req.setUsesLanguageCorrection_(False)
    except Exception:
        pass
    handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []):
        try:
            cand = obs.topCandidates_(1)[0]
            bb = obs.boundingBox()           # normalised, origin BOTTOM-left
            oy, hh = float(bb.origin.y), float(bb.size.height)
            cy = 1.0 - (oy + hh / 2.0)       # -> top-down fraction
            conf = float(getattr(cand, "confidence", lambda: 1.0)()) \
                if callable(getattr(cand, "confidence", None)) \
                else float(getattr(obs, "confidence", 1.0))
            out.append({"t": str(cand.string()), "cy": cy, "conf": conf,
                        "h": hh})
        except Exception:
            continue
    out.sort(key=lambda d: d["cy"])          # top -> bottom
    return out


def _cards_from_lines(lines, ore_names):
    """Group OCR lines into ITEM CARDS in the finds stack. Each card carries a
    weight ('NNN kg'), a name and (usually) a rarity word on nearby lines; the
    stack holds several fading cards at once, so we anchor on each 'kg' line
    (exactly one per card) and attach the nearest name/rarity lines to it.

    Returns cards ordered TOP -> BOTTOM: each {name, mod, kg, rarity, cy, conf}.
    A card whose weight can't be read this frame is skipped (it's read on other
    frames while it lingers in the stack)."""
    if not lines:
        return []
    hs = [d["h"] for d in lines if d["h"] > 0]
    line_h = sorted(hs)[len(hs) // 2] if hs else 0.05
    win = max(2.6 * line_h, 0.12)            # a card spans ~2-3 text lines
    rar_low = {r.lower(): r for r in _RARITIES}
    cards = []
    for d in lines:
        t = (d["t"] or "").strip()
        if "$" in t or t.startswith("+"):
            continue
        m = _KG_RE.search(t)
        if not m:
            continue
        try:
            kg = float(m.group(1).replace(",", "").replace(" ", ""))
        except ValueError:
            continue
        if kg <= 0 or kg > 100000:
            continue
        cy = d["cy"]
        # name: text before 'kg' on this line, else nearest wordy line ABOVE
        inline = _KG_RE.sub("", t).strip(" -\u00b7:")
        name_txt, rarity = "", ""
        if sum(c.isalpha() for c in inline) >= 3 \
                and inline.lower() not in rar_low:
            name_txt = inline
        best_name_d = 9.0
        best_rar_d = 9.0
        for e in lines:
            dy = abs(e["cy"] - cy)
            if dy > win:
                continue
            et = (e["t"] or "").strip()
            low = et.lower()
            if low in rar_low and dy < best_rar_d:
                rarity, best_rar_d = rar_low[low], dy
            elif not name_txt or e["cy"] <= cy:
                clean = _KG_RE.sub("", et).strip(" -\u00b7:")
                if (sum(c.isalpha() for c in clean) >= 3
                        and clean.lower() not in rar_low
                        and "$" not in clean and not clean.startswith("+")):
                    # prefer the line just above the kg (dy small, above)
                    score = dy + (0.0 if e["cy"] <= cy else 0.5)
                    if score < best_name_d:
                        name_txt, best_name_d = clean, score
        if not name_txt:
            continue
        name, mod = _snap_name(name_txt, ore_names)
        if not name:
            continue
        if not rarity:
            rarity = ""              # filled from the price table later by name
        cards.append({"name": name, "mod": mod, "kg": kg, "rarity": rarity,
                      "cy": cy, "conf": d["conf"]})
    cards.sort(key=lambda c: c["cy"])        # top -> bottom
    return cards


def _card_same(a, b):
    """Fuzzy equality of two card reads (same physical card across frames):
    same ore name, weight within 12%, and modifier agrees (or one is blank)."""
    if a["name"] != b["name"]:
        return False
    ka, kb = a["kg"], b["kg"]
    if ka > 0 and kb > 0 and abs(ka - kb) > 0.12 * max(ka, kb):
        return False
    if a["mod"] and b["mod"] and a["mod"] != b["mod"]:
        return False
    return True


# which per-event toggle gates each event name
_EVENT_FLAG = {
    "start": "NOTIFY_START",
    "stop": "NOTIFY_STOP", "autostop": "NOTIFY_STOP", "bag_full": "NOTIFY_STOP",
    "stats": "NOTIFY_STATS",
    "safe_stop": "NOTIFY_SAFE_STOP",
    "recovery": "NOTIFY_RECOVERIES",
    "error": "NOTIFY_ERRORS",
}


