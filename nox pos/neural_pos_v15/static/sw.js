/**
 * Neural POS — Service Worker
 * Provides offline capability via Cache API + IndexedDB queue sync.
 *
 * Strategy:
 *  - Shell (dashboard HTML, fonts, static assets) → Cache First
 *  - API GET requests (products, settings)         → Network First, fall back to cache
 *  - API POST /api/pos/sale                        → Network, queue to IndexedDB on failure
 *  - Everything else                               → Network only (no cache)
 */

const VERSION      = 'neural-pos-v1';
const SHELL_CACHE  = `${VERSION}-shell`;
const API_CACHE    = `${VERSION}-api`;
const IDB_NAME     = 'neural-pos-offline';
const IDB_VERSION  = 1;
const QUEUE_STORE  = 'sale_queue';

// Assets to pre-cache on install (app shell)
const SHELL_ASSETS = [
  '/dashboard',
  '/static/sw.js',
];

// API paths to cache for offline reads
const CACHEABLE_API = [
  '/api/pos/products',
  '/api/settings',
  '/api/hardware/config',
];

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k.startsWith('neural-pos-') && k !== SHELL_CACHE && k !== API_CACHE)
            .map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin requests
  if (url.origin !== location.origin) return;

  const path = url.pathname;

  // POST /api/pos/sale — queue offline
  if (request.method === 'POST' && path === '/api/pos/sale') {
    event.respondWith(handleSalePost(request));
    return;
  }

  // Cacheable API GETs — network first, fallback to cache
  if (request.method === 'GET' && CACHEABLE_API.some(p => path.startsWith(p))) {
    event.respondWith(networkFirstApi(request));
    return;
  }

  // Shell pages — cache first
  if (request.method === 'GET' && (path === '/dashboard' || path === '/')) {
    event.respondWith(cacheFirstShell(request));
    return;
  }

  // Static assets — cache first
  if (request.method === 'GET' && path.startsWith('/static/')) {
    event.respondWith(cacheFirstShell(request));
    return;
  }

  // Everything else — network only (don't intercept)
});

// ── Strategy: Network first, cache on success ─────────────────────────────────
async function networkFirstApi(request) {
  try {
    const response = await fetch(request.clone());
    if (response.ok) {
      const cache = await caches.open(API_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ error: 'offline', cached: false }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

// ── Strategy: Cache first, network fallback ───────────────────────────────────
async function cacheFirstShell(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request.clone());
    if (response.ok) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // Return the cached dashboard as fallback for all navigation
    const fallback = await caches.match('/dashboard');
    return fallback || new Response('Offline — please reload when connected', { status: 503 });
  }
}

// ── Sale POST: try network, queue to IndexedDB on failure ─────────────────────
async function handleSalePost(request) {
  try {
    const response = await fetch(request.clone());
    if (response.ok) return response;
    // Server error — queue for retry
    const body = await request.clone().json();
    await queueSaleIDB(body);
    return offlineQueuedResponse(body);
  } catch {
    // Network failure — queue
    try {
      const body = await request.clone().json();
      await queueSaleIDB(body);
      return offlineQueuedResponse(body);
    } catch {
      return new Response(JSON.stringify({ error: 'offline_queue_failed' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' }
      });
    }
  }
}

function offlineQueuedResponse(saleData) {
  // Return a synthetic success so the UI can show a receipt
  const orderNum = `OFFLINE-${Date.now()}`;
  return new Response(JSON.stringify({
    success: true,
    offline_queued: true,
    order_number: orderNum,
    order_id: null,
    subtotal:  saleData._subtotal  || 0,
    tax:       saleData._tax       || 0,
    discount:  saleData.discount   || 0,
    total:     saleData._total     || 0,
    items:     saleData._items     || [],
    payment_method: saleData.payment_method,
    split_amounts:  saleData.split_amounts || null,
    printer_printed: false,
    printer_error:   'offline',
  }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' }
  });
}

// ── IndexedDB helpers ─────────────────────────────────────────────────────────
function openIDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, IDB_VERSION);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(QUEUE_STORE)) {
        const store = db.createObjectStore(QUEUE_STORE, { keyPath: 'id', autoIncrement: true });
        store.createIndex('queued_at', 'queued_at', { unique: false });
      }
    };
    req.onsuccess = e => resolve(e.target.result);
    req.onerror   = e => reject(e.target.error);
  });
}

async function queueSaleIDB(saleData) {
  const db = await openIDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite');
    tx.objectStore(QUEUE_STORE).add({ ...saleData, queued_at: new Date().toISOString() });
    tx.oncomplete = resolve;
    tx.onerror    = e => reject(e.target.error);
  });
}

async function getQueuedSalesIDB() {
  const db = await openIDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readonly');
    const req = tx.objectStore(QUEUE_STORE).getAll();
    req.onsuccess = e => resolve(e.target.result);
    req.onerror   = e => reject(e.target.error);
  });
}

async function deleteSaleIDB(id) {
  const db = await openIDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite');
    tx.objectStore(QUEUE_STORE).delete(id);
    tx.oncomplete = resolve;
    tx.onerror    = e => reject(e.target.error);
  });
}

async function clearQueueIDB() {
  const db = await openIDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite');
    tx.objectStore(QUEUE_STORE).clear();
    tx.oncomplete = resolve;
    tx.onerror    = e => reject(e.target.error);
  });
}

// ── Background sync (when browser comes back online) ─────────────────────────
self.addEventListener('sync', event => {
  if (event.tag === 'sync-sales') {
    event.waitUntil(syncQueuedSales());
  }
});

async function syncQueuedSales() {
  const queued = await getQueuedSalesIDB();
  if (!queued.length) return;

  // Get auth token from all clients
  const clients = await self.clients.matchAll({ type: 'window' });
  let token = null;
  for (const client of clients) {
    // Ask client for token via postMessage
    token = await new Promise(resolve => {
      const ch = new MessageChannel();
      ch.port1.onmessage = e => resolve(e.data?.token || null);
      client.postMessage({ type: 'GET_TOKEN' }, [ch.port2]);
      setTimeout(() => resolve(null), 500);
    });
    if (token) break;
  }

  let synced = 0;
  let failed = 0;

  for (const sale of queued) {
    try {
      // Remove IDB internal fields before sending
      const { id: idbId, queued_at, _subtotal, _tax, _total, _items, ...salePayload } = sale;
      const response = await fetch('/api/pos/sale', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { 'Authorization': `Bearer ${token}` } : {})
        },
        body: JSON.stringify(salePayload)
      });
      if (response.ok) {
        await deleteSaleIDB(idbId);
        synced++;
      } else {
        failed++;
      }
    } catch {
      failed++;
    }
  }

  // Notify all clients of sync result
  for (const client of clients) {
    client.postMessage({ type: 'SYNC_COMPLETE', synced, failed });
  }
}

// ── Message handler (from page → SW) ─────────────────────────────────────────
self.addEventListener('message', event => {
  const { type } = event.data || {};

  if (type === 'TRIGGER_SYNC') {
    event.waitUntil(syncQueuedSales());
  }

  if (type === 'GET_QUEUE_COUNT') {
    getQueuedSalesIDB().then(q => {
      event.ports[0]?.postMessage({ count: q.length });
    });
  }

  if (type === 'GET_QUEUE') {
    getQueuedSalesIDB().then(q => {
      event.ports[0]?.postMessage({ queue: q });
    });
  }

  if (type === 'CLEAR_QUEUE') {
    clearQueueIDB().then(() => {
      event.ports[0]?.postMessage({ cleared: true });
    });
  }
});
