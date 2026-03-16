import { useCallback, useEffect, useRef, useState } from 'react';

const CLOSED = 3;

export default function useWebSocket({ maxReconnectAttempts = 3, onMessage } = {}) {
  const onMessageRef = useRef(onMessage);
  const socketRef = useRef(null);
  const reconnectAttemptsRef = useRef(0);
  const disconnectIntentRef = useRef(false);
  const reconnectTimeoutRef = useRef(null);

  const [lastMessage, setLastMessage] = useState(null);
  const [readyState, setReadyState] = useState(CLOSED);

  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      window.clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
  }, []);

  const disconnect = useCallback(() => {
    disconnectIntentRef.current = true;
    clearReconnectTimer();
    socketRef.current?.close();
  }, [clearReconnectTimer]);

  const connect = useCallback(
    (url) => {
      if (!url) {
        return;
      }

      disconnectIntentRef.current = false;
      clearReconnectTimer();
      socketRef.current?.close();

      const socket = new WebSocket(url);
      socketRef.current = socket;
      setReadyState(socket.readyState);

      socket.onopen = () => {
        reconnectAttemptsRef.current = 0;
        setReadyState(socket.readyState);
      };

      socket.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data);
          setLastMessage(parsed);
          onMessageRef.current?.(parsed);
        } catch (error) {
          console.error('Failed to parse websocket message.', error);
        }
      };

      socket.onerror = () => {
        setReadyState(socket.readyState);
      };

      socket.onclose = () => {
        if (socketRef.current !== socket) {
          return;
        }

        setReadyState(socket.readyState);

        if (disconnectIntentRef.current || reconnectAttemptsRef.current >= maxReconnectAttempts) {
          return;
        }

        reconnectAttemptsRef.current += 1;
        reconnectTimeoutRef.current = window.setTimeout(() => connect(url), 750);
      };
    },
    [clearReconnectTimer, maxReconnectAttempts],
  );

  const send = useCallback((payload) => {
    if (socketRef.current?.readyState !== WebSocket.OPEN) {
      return false;
    }

    socketRef.current.send(JSON.stringify(payload));
    return true;
  }, []);

  useEffect(() => disconnect, [disconnect]);

  return {
    send,
    lastMessage,
    readyState,
    connect,
    disconnect,
  };
}
