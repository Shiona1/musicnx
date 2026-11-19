import type { ConnectionState, SnapshotPayload, WsEvent } from './types';

interface SocketCallbacks {
  onSnapshot: (payload: SnapshotPayload) => void;
  onEvent: (event: WsEvent) => void;
  onStatus: (status: ConnectionState) => void;
}

const RETRY_DELAYS_MS = [500, 1000, 2000, 5000];

/**
 * WebSocket client with exponential backoff and visibility reporting.
 * The server sends a full snapshot on every (re)connect.
 */
export class MusicSocket {
  private socket: WebSocket | null = null;
  private retry = 0;
  private retryTimer: number | undefined;
  private stopped = true;
  private readonly callbacks: SocketCallbacks;

  constructor(callbacks: SocketCallbacks) {
    this.callbacks = callbacks;
    document.addEventListener('visibilitychange', this.handleVisibility);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.retryTimer !== undefined) {
      window.clearTimeout(this.retryTimer);
      this.retryTimer = undefined;
    }
    document.removeEventListener('visibilitychange', this.handleVisibility);
    this.socket?.close();
    this.socket = null;
  }

  private connect(): void {
    this.callbacks.onStatus(this.retry === 0 ? 'connecting' : 'reconnecting');
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${protocol}://${window.location.host}/api/v1/ws`);
    this.socket = socket;

    socket.onopen = () => {
      this.retry = 0;
      this.callbacks.onStatus('online');
      this.sendHello();
    };

    socket.onmessage = (event: MessageEvent<string>) => {
      let message: unknown;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (typeof message !== 'object' || message === null || !('type' in message)) {
        return;
      }
      const typed = message as { type: string; payload: unknown };
      if (typed.type === 'snapshot') {
        this.callbacks.onSnapshot(typed.payload as SnapshotPayload);
        return;
      }
      this.callbacks.onEvent(typed as WsEvent);
    };

    socket.onclose = () => {
      this.socket = null;
      if (this.stopped) return;
      this.callbacks.onStatus('reconnecting');
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      socket.close();
    };
  }

  private scheduleReconnect(): void {
    const delay = RETRY_DELAYS_MS[Math.min(this.retry, RETRY_DELAYS_MS.length - 1)];
    this.retry += 1;
    this.retryTimer = window.setTimeout(() => this.connect(), delay);
  }

  private sendHello(): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(
      JSON.stringify({
        type: 'client.hello',
        payload: { visible: document.visibilityState === 'visible' },
      }),
    );
  }

  private readonly handleVisibility = (): void => {
    this.sendHello();
  };
}
