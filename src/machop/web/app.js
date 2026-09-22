'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const gate = $('gate'), stage = $('stage'), video = $('screen');
  const pinInput = $('pin'), connectBtn = $('go'), msg = $('msg');
  const typer = $('typer'), statEl = $('stat'), canvas = $('canvas');

  const canvas2d = () =>
    canvas.getContext('2d', { alpha: false, desynchronized: true });

  const ICE_SERVERS = [
    { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302'] },
  ];

  const SENTINEL = '\u200b';

  const SENSITIVITIES = [0.25, 0.5, 1, 1.5, 2];
  let sensitivity = 2;
  try {
    const saved = parseFloat(localStorage.getItem('machop.sensitivity'));
    if (SENSITIVITIES.includes(saved)) sensitivity = saved;
  } catch {}

  const ZOOM_LEVELS = [1, 1.5, 2, 3];
  let zoom = 1, panX = 0, panY = 0;

  let selectMode = false;

  const SWIPE_ACTIONS = {
    up: 'mission', down: 'expose', left: 'space-right', right: 'space-left',
  };

  const NAMED_KEYS = {
    Enter: 'Enter', Backspace: 'Backspace', Tab: 'Tab', Escape: 'Escape',
    Delete: 'Delete', ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown',
    ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight',
    Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown',
  };

  function keyCodeForInputType(inputType) {
    switch (inputType) {
      case 'deleteContentBackward': return 'Backspace';
      case 'deleteContentForward': return 'Delete';
      case 'insertLineBreak':
      case 'insertParagraph': return 'Enter';
      default: return null;
    }
  }

  let pc = null, channel = null, audioChannel = null;
  let activeEngine = null;
  let relaySend = null;
  let usingRelay = false;
  let currentPin = '';
  const modifiers = new Set();

  const say = (text, info = false) => {
    msg.textContent = text;
    msg.className = info ? 'info' : '';
  };

  async function connect(pin, options) {
    const opts = options || {};
    if (!pin && !opts.resume) return say('Enter the code from your Mac.');
    currentPin = pin;

    connectBtn.disabled = pinInput.disabled = true;
    if (!opts.resume) everConnected = false;
    if (!opts.quiet) say(opts.resume ? 'Reconnecting…' : 'Connecting…', true);

    try {
      pc = new RTCPeerConnection({ iceServers: ICE_SERVERS, bundlePolicy: 'max-bundle' });

      pc.addEventListener('track', (event) => {
        if (event.track.kind !== 'video') return;
        video.srcObject = event.streams[0];

        if ('playoutDelayHint' in event.receiver) event.receiver.playoutDelayHint = 0;
      });
      pc.addEventListener('connectionstatechange', onConnectionState);

      channel = pc.createDataChannel('input', { ordered: true });
      channel.addEventListener('open', () => {
        attachInputHandlers(video);
        applyTransform();
        sendViewport();
      });
      channel.addEventListener('message', (event) => fromMac(event.data));

      audioChannel = pc.createDataChannel('audio', {
        ordered: false, maxRetransmits: 0,
      });
      audioChannel.binaryType = 'arraybuffer';
      audioChannel.addEventListener('message', (event) => {
        onAudioPayload(new Uint8Array(event.data));
      });

      pc.addTransceiver('video', { direction: 'recvonly' });

      await pc.setLocalDescription(await pc.createOffer());
      await waitForIceGathering(pc);

      const response = await fetch('/offer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pin,
          sdp: pc.localDescription.sdp,
          type: pc.localDescription.type,
        }),
      });

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `Connection refused (${response.status})`);
      }

      await pc.setRemoteDescription(await response.json());
      armConnectDeadline();
      say('');
      gate.style.display = 'none';
      stage.classList.add('live');
      video.play().catch(() => {});
      keepAwake();
      startStats();

      reportVisibility();
      attempts = 0;
      try { localStorage.setItem('machop.seen', '1'); } catch {}
      return true;
    } catch (error) {
      teardown();
      if (opts.quiet) return false;
      showGate(error.message || 'Connection failed');
      return false;
    }
  }

  function fromMac(raw) {
    let message;
    try { message = JSON.parse(raw); } catch { return; }
    if (!message) return;
    if (message.t === 'cb') { onClipboardFromMac(message.v || ''); return; }
    if (message.t === 'au') { onSoundState(message); return; }
    if (message.t === 'pg') onPong(message.i);
  }

  let pingSeq = 0;
  let pingSentAt = new Map();
  let pingTimer = 0;

  function onPong(id) {
    const sent = pingSentAt.get(id);
    if (sent === undefined) return;
    pingSentAt.delete(id);
    const rtt = Math.round(performance.now() - sent);
    statEl.textContent = `relay · ${rtt} ms`;
    statEl.className = rtt > 600 ? 'bad' : rtt > 250 ? 'warn' : '';
  }

  function startRelayPings() {
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      const id = ++pingSeq;
      pingSentAt.set(id, performance.now());

      if (pingSentAt.size > 5) {
        pingSentAt.clear();
        statEl.textContent = 'relay · no reply';
        statEl.className = 'bad';
      }
      send({ t: 'pg', i: id });
    }, 2000);
  }

  function stopRelayPings() {
    clearInterval(pingTimer);
    pingTimer = 0;
    pingSentAt.clear();
  }

  function waitForIceGathering(peer, timeoutMs = 2500) {
    if (peer.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        peer.removeEventListener('icegatheringstatechange', check);
        clearTimeout(timer);
        resolve();
      };
      const check = () => { if (peer.iceGatheringState === 'complete') done(); };
      const timer = setTimeout(done, timeoutMs);
      peer.addEventListener('icegatheringstatechange', check);
    });
  }

  const MAX_RECONNECTS = 4;
  let attempts = 0;
  let reconnectTimer = 0;

  let everConnected = false;

  const CONNECT_DEADLINE_MS = 1600;
  let deadlineTimer = 0;

  function armConnectDeadline() {
    clearTimeout(deadlineTimer);
    deadlineTimer = setTimeout(() => {
      deadlineTimer = 0;
      if (usingRelay || !pc || pc.connectionState === 'connected') return;
      statEl.textContent = 'peer-to-peer timed out';
      statEl.className = 'warn';
      retryOrRelay();
    }, CONNECT_DEADLINE_MS);
  }

  function clearConnectDeadline() {
    clearTimeout(deadlineTimer);
    deadlineTimer = 0;
  }

  function onConnectionState() {
    if (!pc || usingRelay) return;
    const state = pc.connectionState;
    if (state === 'connected') {
      clearConnectDeadline();
      everConnected = true;
      statEl.textContent = '';
      statEl.className = '';
      return;
    }

    if (state === 'disconnected') {
      statEl.textContent = 'reconnecting…';
      statEl.className = 'warn';
      return;
    }
    if (state === 'failed') retryOrRelay();
  }

  async function retryOrRelay() {
    stopStats();
    clearConnectDeadline();
    if (everConnected && attempts < MAX_RECONNECTS && canResume()) {
      attempts += 1;
      statEl.textContent = `reconnecting… (${attempts})`;
      statEl.className = 'warn';
      teardown();
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(async () => {
        reconnectTimer = 0;
        const ok = await connect(currentPin, { resume: true, quiet: true });
        if (!ok) retryOrRelay();
      }, Math.min(800 * 2 ** (attempts - 1), 6000));
      return;
    }
    teardown();

    if (!currentPin) {
      showGate('Peer-to-peer failed. Enter the code to use the backup connection.');
      return;
    }

    usingRelay = true;
    const ok = await connectRelay(currentPin);
    if (!ok) {
      usingRelay = false;
      showGate('');
    }
  }

  function showGate(message) {
    stopStats();
    gate.style.display = '';
    stage.classList.remove('live');
    connectBtn.disabled = pinInput.disabled = false;
    statEl.textContent = '';
    statEl.className = '';
    attempts = 0;
    if (message) say(message);
  }

  function canResume() {
    try { return localStorage.getItem('machop.seen') === '1'; } catch { return false; }
  }

  let statsTimer = 0;

  function stopStats() {
    clearInterval(statsTimer);
    statsTimer = 0;
  }

  function startStats() {
    stopStats();
    statsTimer = setInterval(async () => {
      if (!pc || pc.connectionState !== 'connected') return;
      let report;
      try { report = await pc.getStats(); } catch { return; }
      let rtt = null, fps = null, lost = 0, received = 0;
      report.forEach((entry) => {
        if (entry.type === 'candidate-pair' && entry.nominated &&
            entry.currentRoundTripTime != null) {
          rtt = entry.currentRoundTripTime * 1000;
        }
        if (entry.type === 'inbound-rtp' && entry.kind === 'video') {
          if (entry.framesPerSecond != null) fps = entry.framesPerSecond;
          lost = entry.packetsLost || 0;
          received = entry.packetsReceived || 0;
        }
      });
      const bits = [];
      if (rtt != null) bits.push(`${Math.round(rtt)} ms`);
      if (fps != null) bits.push(`${Math.round(fps)} fps`);
      const lossPct = received ? (100 * lost) / (lost + received) : 0;
      if (lossPct >= 1) bits.push(`${lossPct.toFixed(0)}% loss`);
      statEl.textContent = bits.join(' · ');
      statEl.className =
        (rtt != null && rtt > 400) || lossPct >= 5 ? 'bad'
        : (rtt != null && rtt > 150) || lossPct >= 1 ? 'warn'
        : '';
    }, 1000);
  }

  function teardown() {
    stopRelayPings();
    audioDisable();
    showSound(false);
    if (audioChannel) { try { audioChannel.close(); } catch {} audioChannel = null; }
    if (channel) { try { channel.close(); } catch {} channel = null; }
    if (pc) { try { pc.close(); } catch {} pc = null; }
  }

  const send = (payload) => {
    if (usingRelay) { if (relaySend) relaySend(payload); return; }
    if (channel && channel.readyState === 'open') channel.send(JSON.stringify(payload));
  };

  function createGestureEngine(emit, options) {
    const opts = options || {};
    const TAP_MS = opts.tapMs || 300;
    const MOVE_PX = opts.movePx || 10;
    const SWIPE_PX = opts.swipePx || 45;

    const LONG_PRESS_MS = opts.longPressMs || 550;

    const TWO_FINGER_TAP_MS = opts.twoFingerTapMs || 700;

    const SCROLL_SLOP_PX = opts.scrollSlopPx || 6;

    const pointers = new Map();
    let mode = 'idle';
    let anchor = null;
    let lastCentroid = null;
    let scrollOrigin = null;
    let lastPan = null;
    let scrolling = false;
    let scrollEmitted = false;
    let swipeFired = false;
    let swipeOrigin = null;
    let sawSecondFinger = false;

    let panEnabled = false;

    function centroid() {
      let x = 0, y = 0;
      for (const p of pointers.values()) { x += p.x; y += p.y; }
      return { x: x / pointers.size, y: y / pointers.size };
    }
    const far = (ax, ay, bx, by) => Math.hypot(ax - bx, ay - by) > MOVE_PX;

    function endScroll() {
      if (!scrolling) return;
      scrolling = false;

      emit({ type: 'scroll', dx: 0, dy: 0, phase: 'end' });
    }

    return {
      get mode() { return mode; },
      get pointerCount() { return pointers.size; },
      setPan(on) { panEnabled = !!on; },

      down(id, x, y, t) {
        pointers.set(id, { x, y, startX: x, startY: y, startT: t });
        const count = pointers.size;

        if (count === 1) {
          mode = 'pending';
          anchor = { x, y, t };
          sawSecondFinger = false;
          scrollEmitted = false;
          return;
        }
        if (count === 2) {
          sawSecondFinger = true;
          if (mode === 'drag') {
            emit({ type: 'mouse', action: 'up', x: anchor.x, y: anchor.y, button: 'left' });
          }
          mode = 'scroll';
          lastCentroid = scrollOrigin = centroid();
          return;
        }
        if (count >= 3) {

          endScroll();
          mode = 'swipe';
          swipeFired = false;
          swipeOrigin = centroid();
        }
      },

      move(id, x, y, t) {
        const p = pointers.get(id);
        if (!p) return;
        p.x = x; p.y = y;

        if (mode === 'swipe') {
          if (swipeFired || pointers.size < 3) return;
          const c = centroid();
          const dx = c.x - swipeOrigin.x;
          const dy = c.y - swipeOrigin.y;
          if (Math.max(Math.abs(dx), Math.abs(dy)) < SWIPE_PX) return;
          swipeFired = true;
          const horizontal = Math.abs(dx) > Math.abs(dy);
          emit({
            type: 'swipe',
            direction: horizontal ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up'),
          });
          return;
        }
        if (mode === 'scroll') {
          if (pointers.size !== 2) return;
          const c = centroid();

          if (!scrolling &&
              Math.hypot(c.x - scrollOrigin.x, c.y - scrollOrigin.y) <= SCROLL_SLOP_PX) {
            return;
          }
          const dx = c.x - lastCentroid.x;
          const dy = c.y - lastCentroid.y;
          lastCentroid = c;
          if (!dx && !dy) return;
          emit({ type: 'scroll', dx, dy, phase: scrolling ? 'move' : 'begin' });
          scrolling = true;
          scrollEmitted = true;
          return;
        }
        if (mode === 'pan') {
          emit({ type: 'pan', dx: x - lastPan.x, dy: y - lastPan.y });
          lastPan = { x, y };
          return;
        }
        if (mode === 'pending') {
          if (!far(x, y, anchor.x, anchor.y)) return;
          if (panEnabled) {
            mode = 'pan';
            lastPan = { x: anchor.x, y: anchor.y };
            emit({ type: 'pan', dx: x - anchor.x, dy: y - anchor.y });
            lastPan = { x, y };
            return;
          }
          emit({ type: 'mouse', action: 'down', x: anchor.x, y: anchor.y, button: 'left' });
          mode = 'drag';
          emit({ type: 'mouse', action: 'drag', x, y, button: 'left' });
          return;
        }
        if (mode === 'drag') emit({ type: 'mouse', action: 'drag', x, y, button: 'left' });
      },

      up(id, x, y, t) {
        const p = pointers.get(id);
        pointers.delete(id);

        if (mode === 'swipe') {
          if (pointers.size === 0) { mode = 'idle'; swipeFired = false; }
          return;
        }
        if (mode === 'pan') {
          if (pointers.size === 0) mode = 'idle';
          return;
        }
        if (mode === 'drag') {
          if (pointers.size === 0) {
            emit({ type: 'mouse', action: 'up', x, y, button: 'left' });
            mode = 'idle';
          }
          return;
        }
        if (mode === 'scroll') {
          if (pointers.size > 0) return;
          endScroll();
          const brief = p && (t - p.startT) < TWO_FINGER_TAP_MS;
          if (brief && !scrollEmitted) {
            emit({ type: 'mouse', action: 'down', x, y, button: 'right' });
            emit({ type: 'mouse', action: 'up', x, y, button: 'right' });
          }
          scrollEmitted = false;
          mode = 'idle';
          return;
        }
        if (mode === 'pending' && pointers.size === 0) {
          const held = p ? t - p.startT : 0;
          const still = p && !far(x, y, p.startX, p.startY);
          if (still && !sawSecondFinger) {
            if (held >= LONG_PRESS_MS) {
              emit({ type: 'mouse', action: 'down', x: anchor.x, y: anchor.y, button: 'right' });
              emit({ type: 'mouse', action: 'up', x: anchor.x, y: anchor.y, button: 'right' });
            } else if (held < TAP_MS) {
              emit({ type: 'mouse', action: 'down', x: anchor.x, y: anchor.y, button: 'left' });
              emit({ type: 'mouse', action: 'up', x: anchor.x, y: anchor.y, button: 'left' });
            }
          }
          mode = 'idle';
        }
      },

      cancel(id) {
        pointers.delete(id);
        if (pointers.size === 0) {
          endScroll();
          if (mode === 'drag') {
            emit({ type: 'mouse', action: 'up', x: anchor.x, y: anchor.y, button: 'left' });
          }
          mode = 'idle';
          swipeFired = false;
        }
      },
    };
  }

  function normalise(clientX, clientY) {
    const surface = usingRelay ? canvas : video;
    const rect = surface.getBoundingClientRect();
    const srcW = usingRelay ? canvas.width : video.videoWidth;
    const srcH = usingRelay ? canvas.height : video.videoHeight;
    if (!srcW || !srcH || !rect.width || !rect.height) return null;

    const videoAspect = srcW / srcH;
    const boxAspect = rect.width / rect.height;
    let w = rect.width, h = rect.height, dx = 0, dy = 0;
    if (videoAspect > boxAspect) { h = rect.width / videoAspect; dy = (rect.height - h) / 2; }
    else { w = rect.height * videoAspect; dx = (rect.width - w) / 2; }

    const x = (clientX - rect.left - dx) / w;
    const y = (clientY - rect.top - dy) / h;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y };
  }

  function attachInputHandlers(surface) {
    const target = surface || video;
    let pending = null, rafId = 0, scrollX = 0, scrollY = 0;
    let mouseDown = false, mouseButton = 'left';

    const flush = () => {
      rafId = 0;
      if (pending) {
        send({ t: 'm', a: pending.a, x: pending.x, y: pending.y, b: pending.b });
        pending = null;
      }
      if (scrollX || scrollY) {
        send({ t: 's', dx: scrollX, dy: scrollY, p: 'move' });
        scrollX = scrollY = 0;
      }
    };
    const schedule = () => { if (!rafId) rafId = requestAnimationFrame(flush); };
    const queueMove = (a, x, y, b) => { pending = { a, x, y, b }; schedule(); };
    const queueScroll = (dx, dy) => { scrollX += dx; scrollY += dy; schedule(); };
    const flushNow = () => {
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      flush();
    };

    const engine = activeEngine = createGestureEngine((action) => {
      if (action.type === 'scroll') {

        if (action.phase === 'move') {
          queueScroll(action.dx * sensitivity, action.dy * sensitivity);
        } else {
          flushNow();
          send({ t: 's', dx: action.dx * sensitivity, dy: action.dy * sensitivity,
                 p: action.phase });
        }
        return;
      }
      if (action.type === 'pan') {
        panBy(action.dx, action.dy);
        return;
      }
      if (action.type === 'swipe') {
        const gesture = SWIPE_ACTIONS[action.direction];
        if (gesture) { flushNow(); send({ t: 'g', g: gesture }); }
        return;
      }
      const point = normalise(action.x, action.y);
      if (!point) return;
      if (action.action === 'drag' || action.action === 'move') {
        queueMove(action.action, point.x, point.y, action.button);
      } else {

        flushNow();
        send({ t: 'm', a: action.action, x: point.x, y: point.y, b: action.button });
      }
    });

    const isTouch = (event) => event.pointerType === 'touch';

    const buttonName = (event) =>
      event.button === 2 ? 'right' : event.button === 1 ? 'middle' : 'left';

    target.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      if (!menu.hidden) {

        closeMenu();
        return;
      }
      if (switcherOpen) {

        commitSwitcher();
        return;
      }
      target.setPointerCapture?.(event.pointerId);
      if (isTouch(event)) {
        engine.down(event.pointerId, event.clientX, event.clientY, event.timeStamp);
        return;
      }
      const point = normalise(event.clientX, event.clientY);
      if (!point) return;
      mouseDown = true;
      mouseButton = buttonName(event);
      send({ t: 'm', a: 'down', x: point.x, y: point.y, b: mouseButton });
    });

    target.addEventListener('pointermove', (event) => {
      if (isTouch(event)) {
        engine.move(event.pointerId, event.clientX, event.clientY, event.timeStamp);
        return;
      }
      const point = normalise(event.clientX, event.clientY);
      if (point) queueMove(mouseDown ? 'drag' : 'move', point.x, point.y, mouseButton);
    }, { passive: true });

    const release = (event) => {
      if (isTouch(event)) {
        engine.up(event.pointerId, event.clientX, event.clientY, event.timeStamp);
        return;
      }
      if (!mouseDown) return;
      mouseDown = false;
      flushNow();
      const point = normalise(event.clientX, event.clientY);
      if (point) send({ t: 'm', a: 'up', x: point.x, y: point.y, b: mouseButton });
    };
    target.addEventListener('pointerup', release);
    target.addEventListener('pointercancel', (event) => {
      if (isTouch(event)) engine.cancel(event.pointerId);
      else release(event);
    });

    target.addEventListener('contextmenu', (event) => event.preventDefault());

    target.addEventListener('wheel', (event) => {
      event.preventDefault();

      const scale = event.deltaMode === 1 ? 16 : 1;
      queueScroll(-event.deltaX * scale, -event.deltaY * scale);
    }, { passive: false });

    window.addEventListener('keydown', onKey);
    window.addEventListener('keyup', onKey);

    typer.value = SENTINEL;

    typer.addEventListener('keydown', (event) => {
      const code = NAMED_KEYS[event.key];
      if (!code) return;
      event.preventDefault();
      send({ t: 'k', c: code, d: true });
      send({ t: 'k', c: code, d: false });
      releaseModifiers();
      typer.value = SENTINEL;
    });

    typer.addEventListener('beforeinput', (event) => {
      const code = keyCodeForInputType(event.inputType);
      if (!code) return;
      event.preventDefault();
      send({ t: 'k', c: code, d: true });
      send({ t: 'k', c: code, d: false });
      typer.value = SENTINEL;
    });

    typer.addEventListener('input', () => {
      const typed = typer.value.split(SENTINEL).join('');
      if (typed) {
        send({ t: 'x', v: typed });

        releaseModifiers();
      }
      typer.value = SENTINEL;
    });

    statEl.textContent = '';
  }

  function onKey(event) {
    if (event.target === typer) return;
    if (!event.code) return;
    event.preventDefault();
    send({ t: 'k', c: event.code, d: event.type === 'keydown' });
  }

  document.querySelectorAll('[data-mod]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const code = btn.dataset.mod;
      const held = modifiers.has(code);
      if (held) { modifiers.delete(code); send({ t: 'k', c: code, d: false }); }
      else { modifiers.add(code); send({ t: 'k', c: code, d: true }); }
      btn.classList.toggle('on', !held);
    });
  });

  document.querySelectorAll('[data-key]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const code = btn.dataset.key;
      send({ t: 'k', c: code, d: true });
      send({ t: 'k', c: code, d: false });
      releaseModifiers();
    });
  });

  function sendCombo(codes) {
    codes.forEach((code) => send({ t: 'k', c: code, d: true }));
    [...codes].reverse().forEach((code) => send({ t: 'k', c: code, d: false }));
  }

  document.querySelectorAll('[data-combo]').forEach((btn) => {
    btn.addEventListener('click', () => {
      sendCombo(btn.dataset.combo.split('+'));
      releaseModifiers();
    });
  });

  document.querySelectorAll('[data-chord]').forEach((btn) => {
    btn.addEventListener('click', () => {
      send({ t: 'c', k: btn.dataset.chord.split('+') });
      releaseModifiers();
    });
  });

  const switcher = $('switch');
  let switcherOpen = false;
  let switcherTimer = 0;

  function commitSwitcher() {
    if (!switcherOpen) return;
    switcherOpen = false;
    clearTimeout(switcherTimer);
    switcher.classList.remove('on');
    send({ t: 'k', c: 'MetaLeft', d: false });
  }

  function armSwitcherCommit() {
    clearTimeout(switcherTimer);
    switcherTimer = setTimeout(commitSwitcher, 1200);
  }

  switcher.addEventListener('click', () => {
    if (!switcherOpen) {
      switcherOpen = true;
      switcher.classList.add('on');
      send({ t: 'c', k: ['MetaLeft', 'Tab'], h: true });
    } else {
      send({ t: 'k', c: 'Tab', d: true });
      send({ t: 'k', c: 'Tab', d: false });
    }
    armSwitcherCommit();
  });

  const quality = $('quality');
  quality.addEventListener('click', () => {
    const next = quality.dataset.profile === 'gui' ? 'text' : 'gui';
    quality.dataset.profile = next;
    quality.textContent = next;
    quality.classList.toggle('on', next === 'text');
    send({ t: 'q', p: next });
  });

  function releaseModifiers() {
    modifiers.forEach((code) => send({ t: 'k', c: code, d: false }));
    modifiers.clear();
    document.querySelectorAll('[data-mod]').forEach((b) => b.classList.remove('on'));
  }

  const surfaceEl = () => (usingRelay ? canvas : video);

  function applyTransform() {
    const el = surfaceEl();
    const rect = stage.getBoundingClientRect();
    const maxX = (rect.width * (zoom - 1)) / 2;
    const maxY = (rect.height * (zoom - 1)) / 2;
    panX = Math.min(maxX, Math.max(-maxX, panX));
    panY = Math.min(maxY, Math.max(-maxY, panY));
    el.style.transform =
      zoom === 1 ? '' : `translate(${panX}px, ${panY}px) scale(${zoom})`;
    syncPanMode();
  }

  function syncPanMode() {
    if (activeEngine) activeEngine.setPan(zoom > 1 && !selectMode);
  }

  function panBy(dx, dy) {
    panX += dx;
    panY += dy;
    applyTransform();
  }

  const VIEWPORT_SETTLE_MS = 600;

  function viewportPixels() {
    const rect = stage.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = rect.width || window.innerWidth || 0;
    const height = rect.height || window.innerHeight || 0;
    return {
      w: Math.round(width * dpr * zoom),
      h: Math.round(height * dpr * zoom),
    };
  }

  let viewportTimer = 0;
  function sendViewport() {
    clearTimeout(viewportTimer);
    viewportTimer = setTimeout(() => {
      const size = viewportPixels();
      send({ t: 'v', w: size.w, h: size.h });
    }, VIEWPORT_SETTLE_MS);
  }

  const zoomBtn = $('zoom');
  zoomBtn.addEventListener('click', () => {
    const next = ZOOM_LEVELS[(ZOOM_LEVELS.indexOf(zoom) + 1) % ZOOM_LEVELS.length];

    const scale = next / zoom;
    panX *= scale;
    panY *= scale;
    zoom = next;
    zoomBtn.textContent = zoom === 1 ? '1:1' : `${zoom}\u00d7`;
    zoomBtn.classList.toggle('on', zoom !== 1);
    applyTransform();
    sendViewport();
  });

  window.addEventListener('resize', () => { applyTransform(); sendViewport(); });
  window.addEventListener('orientationchange', () => { applyTransform(); sendViewport(); });

  const toast = $('toast');
  let toastTimer = 0;
  function say2(text) {
    toast.textContent = text;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 1800);
  }

  const sheet = $('sheet'), sheetText = $('sheetText');
  const sheetMsg = $('sheetMsg'), sheetGo = $('sheetGo');
  let sheetAction = null;

  function openSheet({ message, value, action, go }) {
    sheetMsg.textContent = message;
    sheetText.value = value;
    sheetGo.textContent = go;
    sheetAction = action;
    sheet.hidden = false;
    closeMenu();

    requestAnimationFrame(() => {
      sheetText.focus();
      if (value) sheetText.select();
    });
  }

  function closeSheet() {
    sheet.hidden = true;
    sheetAction = null;
  }

  $('sheetClose').addEventListener('click', closeSheet);
  sheet.addEventListener('click', (event) => {
    if (event.target === sheet) closeSheet();
  });
  sheetGo.addEventListener('click', async () => {
    const action = sheetAction;
    const text = sheetText.value;
    if (action === 'copy') {

      try {
        await navigator.clipboard.writeText(text);
        closeSheet();
        say2(`Copied ${text.length} characters`);
      } catch {
        sheetMsg.textContent =
          'This browser will not let the page copy. Long-press the text above and choose Copy.';
      }
      return;
    }
    if (!text) { say2('Nothing to paste'); return; }
    send({ t: 'cb', a: 'paste', v: text });
    closeSheet();
    say2(`Sent ${text.length} characters`);
  });

  $('copy').addEventListener('click', () => {
    send({ t: 'cb', a: 'copy' });
    say2('Copying…');
  });

  async function onClipboardFromMac(text) {
    if (!text) { say2('Nothing was copied'); return; }
    try {
      await navigator.clipboard.writeText(text);
      say2(`Copied ${text.length} characters`);
    } catch {

      openSheet({
        message: 'Copied from the Mac. Tap Copy to put it on this device.',
        value: text,
        action: 'copy',
        go: 'Copy',
      });
    }
  }

  $('paste').addEventListener('click', async () => {
    let text = '';
    try {
      text = await navigator.clipboard.readText();
    } catch {
      openSheet({
        message: 'Long-press below and choose Paste, then send it to the Mac.',
        value: '',
        action: 'paste',
        go: 'Send to Mac',
      });
      return;
    }
    if (!text) { say2('Clipboard is empty'); return; }
    send({ t: 'cb', a: 'paste', v: text });
    say2(`Pasted ${text.length} characters`);
  });

  const menu = $('menu'), moreBtn = $('more');

  function closeMenu() {
    menu.hidden = true;
    moreBtn.classList.remove('on');
    moreBtn.setAttribute('aria-expanded', 'false');
  }

  moreBtn.addEventListener('click', () => {
    const open = menu.hidden;
    menu.hidden = !open;
    moreBtn.classList.toggle('on', open);
    moreBtn.setAttribute('aria-expanded', String(open));
  });

  const selectBtn = $('select');
  selectBtn.addEventListener('click', () => {
    selectMode = !selectMode;
    selectBtn.classList.toggle('on', selectMode);
    syncPanMode();
    say2(selectMode
      ? 'Drag to select. Tap copy when done.'
      : 'Back to normal');
  });

  const sens = $('sens');
  const showSensitivity = () => { sens.textContent = `${sensitivity}\u00d7`; };
  showSensitivity();
  sens.addEventListener('click', () => {
    const next = SENSITIVITIES[(SENSITIVITIES.indexOf(sensitivity) + 1) % SENSITIVITIES.length];
    sensitivity = next;
    showSensitivity();
    try { localStorage.setItem('machop.sensitivity', String(next)); } catch {}
  });

  $('full').addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else stage.requestFullscreen?.().catch(() => {});
  });

  let wakeLock = null;
  async function keepAwake() {
    try { wakeLock = await navigator.wakeLock?.request('screen'); } catch {}
  }

  const HIDE_GRACE_MS = 3000;
  let hasBeenVisible = false;
  let hideTimer = 0;

  function reportVisibility() {
    const visible = document.visibilityState === 'visible';
    if (visible) {
      hasBeenVisible = true;
      clearTimeout(hideTimer);
      hideTimer = 0;
      send({ t: 'pv', on: true });
      return;
    }
    if (!hasBeenVisible || hideTimer) return;
    hideTimer = setTimeout(() => {
      hideTimer = 0;
      if (document.visibilityState !== 'visible') send({ t: 'pv', on: false });
    }, HIDE_GRACE_MS);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !wakeLock) keepAwake();
    reportVisibility();
  });

  $('kb').addEventListener('click', () => typer.focus());

  const AUDIO_RATE = 48000;
  const AUDIO_CHANNELS = 2;

  const WORKLET_SOURCE = `
    class MachopPlayer extends AudioWorkletProcessor {
      constructor(options) {
        super();
        const o = options.processorOptions || {};
        this.channels = o.channels || 2;
        this.capacity = o.capacity || 48000;
        this.target = o.target || 2400;
        this.maxDepth = o.maxDepth || 12000;
        this.ring = [];
        for (let c = 0; c < this.channels; c++) {
          this.ring.push(new Float32Array(this.capacity));
        }
        this.readPos = 0;
        this.writePos = 0;
        this.playing = false;
        this.port.onmessage = (event) => {
          const planes = event.data;
          if (planes === 'flush') {
            this.readPos = this.writePos = 0;
            this.playing = false;
            return;
          }
          const n = planes[0].length;
          for (let i = 0; i < n; i++) {
            const slot = (this.writePos + i) % this.capacity;
            for (let c = 0; c < this.channels; c++) {
              this.ring[c][slot] = planes[Math.min(c, planes.length - 1)][i];
            }
          }
          this.writePos += n;
          if (this.writePos - this.readPos > this.maxDepth) {
            this.readPos = this.writePos - this.target;
          }
        };
      }
      process(inputs, outputs) {
        const out = outputs[0];
        if (!out || !out.length) return true;
        if (!this.playing) {
          if (this.writePos - this.readPos < this.target) {
            for (const channel of out) channel.fill(0);
            return true;
          }
          this.playing = true;
        }
        for (let i = 0; i < out[0].length; i++) {
          if (this.readPos >= this.writePos) {
            for (let c = 0; c < out.length; c++) out[c][i] = 0;
            this.playing = false;
            continue;
          }
          const slot = this.readPos % this.capacity;
          for (let c = 0; c < out.length; c++) {
            out[c][i] = this.ring[Math.min(c, this.channels - 1)][slot];
          }
          this.readPos++;
        }
        return true;
      }
    }
    registerProcessor('machop-player', MachopPlayer);`;

  const soundBtn = $('sound');
  let audioCtx = null, audioNode = null, audioDecoder = null;
  let audioWanted = false, audioReady = false;
  let audioSeen = 0, audioBlockedWarned = false, audioFaultReported = false;

  const showSound = (on) => {
    soundBtn.classList.toggle('on', on);
    soundBtn.title = on ? 'Stop sharing the Mac’s sound' : 'Play the Mac’s sound';
  };

  function opusHead(channels) {
    const head = new Uint8Array(19);
    head.set(encoder.encode('OpusHead'), 0);
    head[8] = 1;
    head[9] = channels;
    new DataView(head.buffer).setUint32(12, AUDIO_RATE, true);
    return head;
  }

  function audioDataToPlanes(data) {
    const frames = data.numberOfFrames;
    const channels = Math.min(data.numberOfChannels, AUDIO_CHANNELS);
    const planes = [];
    try {
      for (let c = 0; c < channels; c++) {
        const plane = new Float32Array(frames);
        data.copyTo(plane, { planeIndex: c, format: 'f32-planar' });
        planes.push(plane);
      }
      return planes;
    } catch {
      planes.length = 0;
    }
    const format = String(data.format || '');
    const planar = format.endsWith('-planar');
    const scale = format.startsWith('s16') ? 1 / 32768 : 1;
    const Native = format.startsWith('s16') ? Int16Array : Float32Array;
    if (planar) {
      for (let c = 0; c < channels; c++) {
        const raw = new Native(frames);
        data.copyTo(raw, { planeIndex: c });
        const plane = new Float32Array(frames);
        for (let i = 0; i < frames; i++) plane[i] = raw[i] * scale;
        planes.push(plane);
      }
      return planes;
    }
    const raw = new Native(frames * data.numberOfChannels);
    data.copyTo(raw, { planeIndex: 0 });
    for (let c = 0; c < channels; c++) {
      const plane = new Float32Array(frames);
      for (let i = 0; i < frames; i++) plane[i] = raw[i * data.numberOfChannels + c] * scale;
      planes.push(plane);
    }
    return planes;
  }

  function resamplePlanes(planes, ratio) {
    const out = [];
    for (const plane of planes) {
      const length = Math.max(1, Math.round(plane.length * ratio));
      const scaled = new Float32Array(length);
      for (let i = 0; i < length; i++) {
        const at = i / ratio;
        const low = Math.min(plane.length - 1, Math.floor(at));
        const high = Math.min(plane.length - 1, low + 1);
        const t = at - low;
        scaled[i] = plane[low] * (1 - t) + plane[high] * t;
      }
      out.push(scaled);
    }
    return out;
  }

  function playDecoded(data) {
    try {
      if (!audioNode) return;
      let planes = audioDataToPlanes(data);
      const rate = data.sampleRate || AUDIO_RATE;
      if (audioCtx && Math.abs(audioCtx.sampleRate - rate) > 1) {
        planes = resamplePlanes(planes, audioCtx.sampleRate / rate);
      }
      audioNode.port.postMessage(planes, planes.map((plane) => plane.buffer));
    } catch (error) {
      if (!audioFaultReported) {
        audioFaultReported = true;
        const what = `sound playback failed: ${error && error.message ? error.message : error}`;
        say2(what);
        send({ t: 'err', m: what });
      }
    } finally {
      data.close();
    }
  }

  async function audioEnable() {
    if (audioReady) return true;
    if (!('AudioDecoder' in window)) {
      say2('This browser cannot play the Mac’s sound (needs WebCodecs).');
      return false;
    }
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: AUDIO_RATE, latencyHint: 'interactive',
      });
    } catch {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }

    nudgeAudio();
    const url = URL.createObjectURL(
      new Blob([WORKLET_SOURCE], { type: 'text/javascript' }));
    try {
      await audioCtx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    audioNode = new AudioWorkletNode(audioCtx, 'machop-player', {
      numberOfInputs: 0,
      outputChannelCount: [AUDIO_CHANNELS],
      processorOptions: {
        channels: AUDIO_CHANNELS,
        capacity: Math.round(audioCtx.sampleRate),
        target: Math.round(audioCtx.sampleRate * 0.05),
        maxDepth: Math.round(audioCtx.sampleRate * 0.25),
      },
    });
    audioNode.connect(audioCtx.destination);

    audioDecoder = new AudioDecoder({
      output: playDecoded,
      error: (error) => {
        say2(`Sound stopped: ${error && error.message ? error.message : error}`);
        audioDisable();
        send({ t: 'au', on: false });
        showSound(false);
      },
    });
    const base = { codec: 'opus', sampleRate: AUDIO_RATE, numberOfChannels: AUDIO_CHANNELS };
    try {
      audioDecoder.configure(base);
    } catch {
      audioDecoder.configure({ ...base, description: opusHead(AUDIO_CHANNELS) });
    }
    audioReady = true;
    return true;
  }

  function nudgeAudio() {
    if (!audioCtx || audioCtx.state === 'running') return;
    try { audioCtx.resume().catch(() => {}); } catch {}
  }

  function audioDisable() {
    audioWanted = false;
    audioReady = false;
    audioSeen = 0;
    audioBlockedWarned = false;
    audioFaultReported = false;
    if (audioDecoder) { try { audioDecoder.close(); } catch {} audioDecoder = null; }
    if (audioNode) { try { audioNode.disconnect(); } catch {} audioNode = null; }
    if (audioCtx) { try { audioCtx.close(); } catch {} audioCtx = null; }
  }

  function onAudioPayload(bytes) {
    if (!audioReady || !audioDecoder || audioDecoder.state !== 'configured') return;
    if (bytes.length <= 8) return;

    if (audioCtx && audioCtx.state !== 'running' && ++audioSeen % 50 === 0) {
      nudgeAudio();
      if (audioSeen >= 100 && !audioBlockedWarned) {
        audioBlockedWarned = true;
        say2('Tap the screen to let this browser play sound');
      }
    }
    const timestamp = Number(
      new DataView(bytes.buffer, bytes.byteOffset, 8).getBigUint64(0));
    try {
      audioDecoder.decode(new EncodedAudioChunk({
        type: 'key', timestamp, data: bytes.slice(8),
      }));
    } catch {

    }
  }

  soundBtn.addEventListener('click', async () => {
    if (audioWanted) {
      audioDisable();
      showSound(false);
      send({ t: 'au', on: false });
      say2('Sound off');
      return;
    }
    if (!await audioEnable()) return;
    audioWanted = true;
    showSound(true);
    send({ t: 'au', on: true });
    say2('Asking the Mac for sound…');
  });

  function onSoundState(message) {
    const on = !!message.on;
    if (!on) { audioDisable(); }
    showSound(on);
    if (message.m) say2(message.m);
    else if (on) say2('Sound on');
  }

  const encoder = new TextEncoder();
  const b64 = {
    decode: (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0)),
    encode: (b) => btoa(String.fromCharCode(...b)),
  };
  const concat = (...parts) => {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const p of parts) { out.set(p, at); at += p.length; }
    return out;
  };

  const TAG_CONFIG = 0, TAG_VIDEO = 1, TAG_INPUT = 2, TAG_READY = 3, TAG_AUDIO = 4;

  async function hkdf(ikm, salt, info, bytes = 32) {
    const key = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
    return new Uint8Array(await crypto.subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt, info: encoder.encode(info) }, key, bytes * 8));
  }

  async function connectRelay(pin) {
    if (!('VideoDecoder' in window)) {
      say('This browser cannot use the fallback connection (needs WebCodecs).');
      return false;
    }
    say('Direct connection failed — using encrypted relay…', true);

    const pair = await crypto.subtle.generateKey(
      { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
    const myPublic = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey));

    const url = new URL('/relay', location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';

    let keys = null, outCounter = 0, decoder = null, context = null;
    let lastConfig = null;

    let needKeyframe = true;

    const DECODE_QUEUE_LIMIT = 2;
    let queueWarned = false;

    let inOrder = Promise.resolve();

    const sendSealed = async (tag, bytes) => {
      if (!keys || socket.readyState !== WebSocket.OPEN) return;
      const counter = new Uint8Array(8);
      new DataView(counter.buffer).setBigUint64(0, BigInt(outCounter++));
      const sealed = new Uint8Array(await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: concat(keys.c2sPrefix, counter) },
        keys.c2s, concat(new Uint8Array([tag]), bytes)));
      socket.send(concat(counter, sealed));
    };

    relaySend = (payload) => { sendSealed(TAG_INPUT, encoder.encode(JSON.stringify(payload))); };

    return new Promise((resolve) => {
      const fail = (message) => { say(message); try { socket.close(); } catch {} resolve(false); };

      socket.onopen = () => {
        const size = viewportPixels();
        socket.send(JSON.stringify({
          pin, pub: b64.encode(myPublic), forced: FORCE_RELAY,
          w: size.w, h: size.h,
        }));
      };

      socket.onmessage = async (event) => {
        if (typeof event.data === 'string') {
          const hello = JSON.parse(event.data);
          const serverPublic = b64.decode(hello.pub), salt = b64.decode(hello.salt);
          const peer = await crypto.subtle.importKey(
            'raw', serverPublic, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
          const shared = new Uint8Array(await crypto.subtle.deriveBits(
            { name: 'ECDH', public: peer }, pair.privateKey, 256));
          const ikm = concat(shared, encoder.encode(pin));

          const macKey = await crypto.subtle.importKey(
            'raw', await hkdf(ikm, salt, 'machop confirm'),
            { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
          const tag = new Uint8Array(await crypto.subtle.sign(
            'HMAC', macKey, concat(myPublic, serverPublic, salt)));
          const expected = b64.decode(hello.confirm);
          if (tag.length !== expected.length || !tag.every((b, i) => b === expected[i])) {
            return fail('Could not verify the Mac — refusing to connect.');
          }

          keys = {
            s2c: await crypto.subtle.importKey('raw', await hkdf(ikm, salt, 'machop s2c'),
              { name: 'AES-GCM' }, false, ['decrypt']),
            c2s: await crypto.subtle.importKey('raw', await hkdf(ikm, salt, 'machop c2s'),
              { name: 'AES-GCM' }, false, ['encrypt']),
            s2cPrefix: await hkdf(ikm, salt, 'machop nonce s2c', 4),
            c2sPrefix: await hkdf(ikm, salt, 'machop nonce c2s', 4),
          };
          await sendSealed(TAG_READY, new Uint8Array(0));
          return;
        }

        if (!keys) return;
        const record = new Uint8Array(event.data);

        inOrder = inOrder.then(() => handleRecord(record)).catch(() => {});
      };

      async function handleRecord(record) {
        const counter = record.slice(0, 8);
        let plain;
        try {
          plain = new Uint8Array(await crypto.subtle.decrypt(
            { name: 'AES-GCM', iv: concat(keys.s2cPrefix, counter) }, keys.s2c, record.slice(8)));
        } catch { return; }

        const tag = plain[0];
        if (tag === TAG_CONFIG) {

          const config = JSON.parse(new TextDecoder().decode(plain.slice(1)));
          const first = decoder === null;
          lastConfig = config;
          await buildDecoder(config);

          if (first) {
            video.hidden = true; canvas.hidden = false;
            gate.style.display = 'none';
            stage.classList.add('live');
            attachInputHandlers(canvas);

            statEl.textContent = 'relay';
            statEl.className = 'warn';
            startRelayPings();
            video.style.transform = '';
            applyTransform();
            sendViewport();
            resolve(true);
          } else {
            applyTransform();
          }
        } else if (tag === TAG_INPUT) {
          fromMac(new TextDecoder().decode(plain.slice(1)));
        } else if (tag === TAG_AUDIO) {
          onAudioPayload(plain.slice(1));
        } else if (tag === TAG_VIDEO && decoder) {
          const isKey = plain[1] === 1;
          const timestamp = Number(new DataView(plain.buffer, plain.byteOffset + 2, 8).getBigUint64(0));
          if (decoder.state !== 'configured') return;

          if (needKeyframe && !isKey) return;

          if (!isKey && decoder.decodeQueueSize > DECODE_QUEUE_LIMIT) {
            if (!queueWarned) {
              queueWarned = true;
              tellMac(`decoder falling behind (${decoder.decodeQueueSize} queued); ` +
                      `dropping frames to stay current`);
            }
            needKeyframe = true;
            sendSealed(TAG_INPUT, encoder.encode(JSON.stringify({ t: 'kf' })));
            return;
          }
          needKeyframe = false;
          try {
            decoder.decode(new EncodedVideoChunk({
              type: isKey ? 'key' : 'delta', timestamp, data: plain.slice(10),
            }));
          } catch {
            recoverDecoder();
          }
        }
      }

      function tellMac(what) {
        say2(what);
        try {
          sendSealed(TAG_INPUT, encoder.encode(JSON.stringify({ t: 'err', m: what })));
        } catch {}
      }

      const DECODER_VARIANTS = [
        (c) => ({ codec: c.codec, optimizeForLatency: true }),
        (c) => ({ codec: c.codec }),
        (c) => ({ codec: c.codec, codedWidth: c.width, codedHeight: c.height }),
        (c) => ({ codec: c.codec, codedWidth: c.width, codedHeight: c.height,
                  hardwareAcceleration: 'prefer-software' }),
      ];
      let variant = 0;
      let generation = 0;
      let reportedWorking = false;

      async function buildDecoder(config) {

        context = canvas2d();

        const mine = ++generation;
        if (decoder) { try { decoder.close(); } catch {} }
        needKeyframe = true;
        const init = DECODER_VARIANTS[variant % DECODER_VARIANTS.length](config);

        try {
          if (typeof VideoDecoder.isConfigSupported === 'function') {
            const support = await VideoDecoder.isConfigSupported(init);
            if (support && support.supported === false) {
              tellMac(`this device will not decode ${config.codec}`);
            }
          }
        } catch {

        }
        if (mine !== generation) return;

        decoder = new VideoDecoder({
          output: (frame) => {
            if (mine !== generation) { frame.close(); return; }
            if (!reportedWorking) {
              reportedWorking = true;
              if (variant > 0) tellMac(`decoding with fallback config ${variant}`);
            }

            if (canvas.width !== frame.displayWidth ||
                canvas.height !== frame.displayHeight) {
              canvas.width = frame.displayWidth;
              canvas.height = frame.displayHeight;
              context = canvas2d();
              applyTransform();
            }
            context.drawImage(frame, 0, 0);
            frame.close();
          },

          error: (error) => {
            if (mine !== generation) return;
            tellMac(`decoder error (config ${variant}): ` +
                    `${error && error.message ? error.message : error}`);
            variant += 1;
            recoverDecoder();
          },
        });

        try {
          decoder.configure(init);
        } catch (error) {
          tellMac(`could not configure ${config.codec} (config ${variant}): ` +
                  `${error.message || error}`);
          variant += 1;
        }
      }

      let recovering = false;
      async function recoverDecoder() {
        if (recovering || !lastConfig) return;
        recovering = true;
        try { await buildDecoder(lastConfig); } finally { recovering = false; }

        sendSealed(TAG_INPUT, encoder.encode(JSON.stringify({ t: 'kf' })));
      }

      socket.onerror = () => fail('Relay connection failed.');
      socket.onclose = (event) => {
        if (!keys) fail(event.reason || 'Relay refused the connection.');
      };
    });
  }

  const connectFromGate = async () => {
    const pin = pinInput.value.trim();
    if (FORCE_RELAY) {
      if (!pin) return say('Enter the code from your Mac.');
      currentPin = pin;
      connectBtn.disabled = pinInput.disabled = true;
      say('Using the encrypted relay…', true);
      usingRelay = true;
      if (!await connectRelay(pin)) { usingRelay = false; showGate(''); }
      return;
    }
    connect(pin);
  };
  connectBtn.addEventListener('click', connectFromGate);
  pinInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') connectFromGate();
  });
  window.addEventListener('pagehide', teardown);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (!stage.classList.contains('live')) return;

    if (usingRelay) return;
    if (!pc || pc.connectionState === 'failed' || pc.connectionState === 'closed') {
      attempts = 0;
      retryOrRelay();
    }
  });

  const FORCE_RELAY = new URLSearchParams(location.search).get('relay') === '1';

  if (!FORCE_RELAY && canResume()) {
    connect('', { resume: true }).then((ok) => { if (!ok) say(''); });
  }
})();
