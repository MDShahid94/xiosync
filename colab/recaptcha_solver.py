#!/usr/bin/env python3
"""
recaptcha_solver.py — XIO Mesh: Isolated reCAPTCHA Audio Solver
================================================================
Reusable tool usable by ANY workflow:
  • Import:  from recaptcha_solver import solve_recaptcha
  • CLI:     python3 recaptcha_solver.py --cdp-port 9300 --out /tmp/rc_result.json

Auto-installs pydub, SpeechRecognition, ffmpeg on first call — callers need
not pre-install anything.

Returns True if reCAPTCHA was solved and the page URL advanced beyond
challenge/recaptcha; False otherwise.
"""
from __future__ import annotations
import os, sys, time, uuid, subprocess, urllib.request

# ──────────────────────────────────────────────────────────────────────────────
# Dependency bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_deps(log_fn=print):
    """Install pydub and SpeechRecognition if missing. Idempotent."""
    missing = []
    try:
        import pydub  # noqa
    except ImportError:
        missing.append('pydub')
    try:
        import speech_recognition  # noqa
    except ImportError:
        missing.append('SpeechRecognition')
    if missing:
        log_fn(f'[recaptcha_solver] Installing: {missing}')
        for pkg in missing:
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', pkg],
                           capture_output=True)
    # Ensure ffmpeg
    if subprocess.run(['which', 'ffmpeg'], capture_output=True).returncode != 0:
        log_fn('[recaptcha_solver] Installing ffmpeg...')
        subprocess.run(['apt-get', 'install', '-y', '-q', 'ffmpeg'],
                       capture_output=True, check=False)

# ──────────────────────────────────────────────────────────────────────────────
# Core solver
# ──────────────────────────────────────────────────────────────────────────────

def solve_recaptcha(driver, log_fn=print, sleep_fn=None, max_attempts: int = 2) -> bool:
    """
    Solve a Google reCAPTCHA on the page currently loaded in *driver*.

    Parameters
    ----------
    driver      : selenium / undetected-chromedriver WebDriver instance
    log_fn      : callable(str)          — logging sink (default: print)
    sleep_fn    : callable(min_s, max_s) — human-like sleep (default: random.uniform)
    max_attempts: int                    — audio-solve retries (default: 2)

    Returns True if URL left challenge/recaptcha, False otherwise.
    """
    _ensure_deps(log_fn)
    from selenium.webdriver.common.by import By

    if sleep_fn is None:
        import random
        sleep_fn = lambda mn, mx: time.sleep(random.uniform(mn, mx))

    # ── helpers ──────────────────────────────────────────────────────────────

    def _url():
        try: return driver.current_url
        except: return ''

    def _bframes():
        return driver.find_elements(By.CSS_SELECTOR,
            "iframe[src*='recaptcha/api2/bframe'],"
            "iframe[src*='recaptcha/enterprise/bframe']")

    def _download_and_transcribe(audio_src):
        uid = uuid.uuid4().hex[:8]
        mp3, wav = f'/tmp/rc_{uid}.mp3', f'/tmp/rc_{uid}.wav'
        try:
            req = urllib.request.Request(audio_src, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Referer': 'https://www.google.com/'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                with open(mp3, 'wb') as f: f.write(resp.read())
        except Exception as e:
            log_fn(f'[recaptcha_solver] Audio DL failed: {e}'); return ''
        try:
            from pydub import AudioSegment
            from pydub.effects import normalize
            from pydub import effects as _pfx
            audio = normalize(AudioSegment.from_mp3(mp3).set_channels(1))
            try:
                audio = _pfx.high_pass_filter(audio, 300)
                audio = _pfx.low_pass_filter(audio, 3400)
            except Exception: pass
            audio.export(wav, format='wav')
        except Exception:
            subprocess.run(['ffmpeg','-y','-i',mp3,wav], capture_output=True, timeout=20)
        try: os.unlink(mp3)
        except: pass

        _DIGITS = frozenset('zero one two three four five six seven eight nine 0 1 2 3 4 5 6 7 8 9'.split())
        def _score(t):
            w = t.lower().split(); return sum(1 for x in w if x in _DIGITS)/max(len(w),1)
        try:
            import speech_recognition as sr
            rec = sr.Recognizer()
            with sr.AudioFile(wav) as src: data = rec.record(src)
            try: os.unlink(wav)
            except: pass
            res = rec.recognize_google(data, show_all=True, language='en-US')
            if isinstance(res, dict) and 'alternative' in res:
                alts = [r.get('transcript','') for r in res['alternative']]
                log_fn(f'[recaptcha_solver] Alts: {alts}')
                return sorted(alts, key=_score, reverse=True)[0].lower().strip() if alts else ''
            return (str(res) if res else '').lower().strip()
        except Exception as e:
            log_fn(f'[recaptcha_solver] STT error: {e}'); return ''

    def _get_audio_src():
        return driver.execute_script(
            "var a=document.querySelector('audio#audio-source,audio.rc-audiochallenge-audio-response');"
            "if(a&&a.src)return a.src;"
            "var s=document.querySelector('#audio-source source,audio source');"
            "if(s&&s.src)return s.src;"
            "var l=document.querySelector('.rc-audiochallenge-tdownload-link,a[href*=payload]');"
            "if(l&&l.href)return l.href; return null;")

    def _solve_audio(label):
        from selenium.webdriver.common.keys import Keys
        bf = _bframes()
        if not bf:
            log_fn(f'[recaptcha_solver] {label}: no bframe — URL={_url()[:60]}')
            return 'no_bframe'
        try:
            driver.switch_to.frame(bf[0])
        except Exception as e:
            log_fn(f'[recaptcha_solver] {label}: frame error: {e}')
            try: driver.switch_to.default_content()
            except: pass
            return False
        sleep_fn(0.6, 1.0)
        # Request fresh challenge
        driver.execute_script(
            "var r=document.querySelector('#recaptcha-reload-button,.rc-button-reload');"
            "if(r){r.click();}")
        sleep_fn(1.0, 1.5)
        # Click audio button
        ab = driver.execute_script(
            "var b=document.querySelector('#recaptcha-audio-button,button.rc-button-audio,"
            "[aria-label*=audio],[aria-label*=Audio]');"
            "if(b){b.click();return true;}return false;")
        log_fn(f'[recaptcha_solver] {label}: audio_btn={ab}')
        if not ab:
            driver.switch_to.default_content(); return False
        # Wait for audio src (up to ~20s — Google audio CDN can be slow from Colab)
        src = None
        for _ in range(40):
            sleep_fn(0.4, 0.6)
            src = _get_audio_src()
            if src: break
        driver.switch_to.default_content()
        if not src:
            log_fn(f'[recaptcha_solver] {label}: audio src timeout'); return False
        log_fn(f'[recaptcha_solver] {label}: src={src[:70]}…')
        answer = _download_and_transcribe(src)
        log_fn(f'[recaptcha_solver] {label}: answer={answer!r}')
        if not answer: return False
        # Submit
        bf2 = _bframes()
        if not bf2:
            log_fn(f'[recaptcha_solver] {label}: bframe gone pre-submit — URL={_url()[:80]}')
            return 'challenge/recaptcha' not in _url()
        try:
            driver.switch_to.frame(bf2[0])
            inp = None
            for _ in range(10):
                inp = driver.execute_script(
                    "return document.querySelector("
                    "'#audio-response,.rc-audiochallenge-response-field input');")
                if inp: break
                sleep_fn(0.4, 0.6)
            if not inp:
                log_fn(f'[recaptcha_solver] {label}: response input missing')
                driver.switch_to.default_content(); return False
            driver.execute_script("arguments[0].value='';", inp)
            inp.send_keys(answer)
            sleep_fn(0.25, 0.4)
            inp.send_keys(Keys.RETURN)
            log_fn(f'[recaptcha_solver] {label}: submitted')
            driver.switch_to.default_content()
        except Exception as e:
            log_fn(f'[recaptcha_solver] {label}: submit error: {e}')
            try: driver.switch_to.default_content()
            except: pass
            return False
        sleep_fn(3.5, 5.0)
        url = _url()
        log_fn(f'[recaptcha_solver] {label}: post-submit URL={url[:100]}')
        if 'challenge/recaptcha' not in url: return True
        if not _bframes(): return True
        log_fn(f'[recaptcha_solver] {label}: bframe still visible — wrong answer')
        return False

    # ── main ─────────────────────────────────────────────────────────────────

    try:
        log_fn('[recaptcha_solver] Checking for reCAPTCHA...')
        bfs = _bframes()
        if not bfs:
            # Try checkbox path via anchor iframe
            driver.execute_script(
                "(function(){var els=document.querySelectorAll('div');"
                "for(var i=0;i<els.length;i++){var s=els[i].style;"
                "if(s.position==='fixed'&&s.zIndex==='2000000000'){els[i].remove();}}})();")
            sleep_fn(0.3, 0.5)
            anchors = driver.find_elements(By.CSS_SELECTOR,
                "iframe[src*='recaptcha/api2/anchor'],"
                "iframe[src*='recaptcha/enterprise/anchor']")
            if not anchors:
                log_fn('[recaptcha_solver] No anchor iframe — no reCAPTCHA on page'); return False
            try:
                driver.switch_to.frame(anchors[0])
                clicked = driver.execute_script(
                    "var cb=document.querySelector('#recaptcha-anchor,.recaptcha-checkbox');"
                    "if(cb){cb.click();return true;}return false;")
                driver.switch_to.default_content()
            except Exception as e:
                log_fn(f'[recaptcha_solver] Anchor error: {e}')
                try: driver.switch_to.default_content()
                except: pass
                return False
            if not clicked:
                log_fn('[recaptcha_solver] Checkbox not found'); return False
            log_fn('[recaptcha_solver] Checkbox clicked — waiting for bframe...')
            sleep_fn(2.5, 3.5)
            if not _bframes():
                url = _url()
                if 'challenge/recaptcha' not in url:
                    log_fn(f'[recaptcha_solver] Passed silently — {url[:60]}'); return True
                return False
        else:
            log_fn(f'[recaptcha_solver] bframe already open ({len(bfs)}) — audio solve')

        # Audio solve attempts
        for i in range(1, max_attempts + 1):
            label = f'Attempt{i}'
            result = _solve_audio(label)
            if result is True: return True
            if result == 'no_bframe':
                url = _url(); return 'challenge/recaptcha' not in url
            if i < max_attempts:
                log_fn(f'[recaptcha_solver] {label} failed — retrying...')

        log_fn(f'[recaptcha_solver] All {max_attempts} attempt(s) exhausted')
        return False

    except Exception as e:
        log_fn(f'[recaptcha_solver] Fatal error: {e}')
        try: driver.switch_to.default_content()
        except: pass
        return False


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry (for Node.js / xb_shell integration)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse, json
    p = argparse.ArgumentParser(description='XIO Mesh reCAPTCHA solver CLI')
    p.add_argument('--cdp-port', type=int, default=9300)
    p.add_argument('--out', default='/tmp/rc_result.json')
    p.add_argument('--attempts', type=int, default=2)
    args = p.parse_args()
    logs = []
    def _log(msg):
        print(msg, flush=True); logs.append(msg)
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.debugger_address = f'127.0.0.1:{args.cdp_port}'
        driver = uc.Chrome(options=opts, use_subprocess=False)
        ok = solve_recaptcha(driver, log_fn=_log, max_attempts=args.attempts)
        out = {'success': ok, 'url': driver.current_url, 'logs': logs}
    except Exception as e:
        _log(f'CLI error: {e}')
        out = {'success': False, 'error': str(e), 'logs': logs}
    with open(args.out, 'w') as f: json.dump(out, f, indent=2)
    print(f'Result → {args.out}')
    sys.exit(0 if out.get('success') else 1)
