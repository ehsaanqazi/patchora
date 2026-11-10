"""Payment gateway client."""

import socket

PAYMENT_GATEWAY = ("payments.example.net", 443)


def charge(order_id: str, amount: float, timeout: float = 5.0) -> str:
    with socket.create_connection(PAYMENT_GATEWAY, timeout=timeout) as conn:
        conn.sendall(f"CHARGE {order_id} {amount:.2f}\n".encode())
        return conn.recv(64).decode().strip()
