// VidRipper Chrome extension — service worker.
//
// Click the toolbar button on any promo page. It detects the embedded video
// (same logic as the old bookmarklet) AND captures the visible top of the page
// (the hero — headline is always above the fold, either as text above the video
// or baked into the video thumbnail), then posts both to VidRipper. Because the
// capture happens in the user's own browser, Cloudflare-gated pages (which block
// our datacenter server) render the real hero — no proxy needed.

const API = "https://vidripper.oxfordhub.app";

// Runs in the PAGE context. Detects the embedded video URL and scrolls to the
// top so the captured viewport shows the hero. Returns {videoUrl, pageUrl}.
function detectVideo() {
  var orig = location.href, url = orig;
  var el = document.querySelector('[class*="wistia_async_"]');
  if (el) {
    var cls = el.className, idx = cls.indexOf('wistia_async_');
    if (idx >= 0) { var id = cls.substring(idx + 13).split(' ')[0]; if (id) url = 'https://fast.wistia.com/medias/' + id; }
  }
  if (url === orig) {
    var bc = document.querySelector('[data-video-id][data-account]') || document.querySelector('video-js[data-video-id]');
    if (bc) {
      var v = bc.getAttribute('data-video-id'), a = bc.getAttribute('data-account'), pl = bc.getAttribute('data-player') || 'default';
      if (v && a) url = 'https://players.brightcove.net/' + a + '/' + pl + '_default/index.html?videoId=' + v;
    }
  }
  if (url === orig) {
    var vel = document.querySelector('[id^="vidalytics_embed_"]');
    if (vel) {
      var vid = vel.id.substring('vidalytics_embed_'.length);
      var vs = document.querySelectorAll('script[src*="fast.vidalytics.com/embeds/"]');
      var acc = '';
      for (var i = 0; i < vs.length; i++) { var pr = vs[i].src.split('/'); var ei = pr.indexOf('embeds'); if (ei >= 0 && pr[ei + 1]) { acc = pr[ei + 1]; break; } }
      if (vid && acc) url = 'https://fast.vidalytics.com/embeds/' + acc + '/' + vid + '/';
      else if (vid) url = 'https://fast.vidalytics.com/embeds/unknown/' + vid + '/';
    }
  }
  if (url === orig) {
    var fr = document.querySelectorAll('iframe');
    for (var j = 0; j < fr.length; j++) { var fs = fr[j].src || ''; if (fs.indexOf('wistia') >= 0 || fs.indexOf('brightcove') >= 0) { url = fs; break; } }
  }
  window.scrollTo(0, 0);
  return { videoUrl: url, pageUrl: orig };
}

function flash(tabId, text, color) {
  try {
    chrome.action.setBadgeText({ tabId, text });
    chrome.action.setBadgeBackgroundColor({ tabId, color: color || '#1f6feb' });
    setTimeout(() => { try { chrome.action.setBadgeText({ tabId, text: '' }); } catch (e) {} }, 4000);
  } catch (e) {}
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || !tab.id) return;
  try {
    flash(tab.id, '…');
    const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: detectVideo });
    const res = (results && results[0] && results[0].result) || {};
    const videoUrl = res.videoUrl, pageUrl = res.pageUrl;
    if (!videoUrl) { flash(tab.id, '✕', '#d1242f'); return; }

    // Let the scroll-to-top settle, then capture the visible viewport (the hero).
    await new Promise((r) => setTimeout(r, 400));
    let shot = null;
    try {
      shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 85 });
    } catch (e) { /* capture may fail on some pages; still submit the video */ }

    const body = { url: videoUrl, page_url: pageUrl };
    if (shot) body.screenshot = shot;
    await fetch(API + '/api/rip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    flash(tab.id, '✓', '#1a7f37');
    chrome.tabs.create({ url: API + '/' });
  } catch (e) {
    flash(tab.id, '✕', '#d1242f');
    chrome.tabs.create({ url: API + '/' });
  }
});
