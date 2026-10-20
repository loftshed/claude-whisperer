import { randomUUID } from "node:crypto";
import { createConnection } from "node:net";

/** No discovery of a user's focused server. Use the socket supplied by Herdr. */
export function herdrClient({ socket = process.env.HERDR_SOCKET_PATH, timeout = 5000 } = {}) {
  if (!socket) throw new Error("Run this command inside Herdr; HERDR_SOCKET_PATH is missing");
  return async (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = randomUUID();
      const connection = createConnection(socket);
      let buffer = "";
      const finish = (error, result) => {
        connection.destroy();
        if (error) reject(error);
        else resolve(result);
      };
      connection.setEncoding("utf8");
      connection.setTimeout(timeout, () =>
        finish(
          new Error(`Herdr ${method} timed out; it may have succeeded. Reconcile before retrying.`),
        ),
      );
      connection.once("error", (error) => finish(error));
      connection.once("connect", () =>
        connection.write(`${JSON.stringify({ id, method, params })}\n`),
      );
      connection.on("data", (chunk) => {
        buffer += chunk;
        if (buffer.length > 8 * 1024 * 1024)
          return finish(new Error("Herdr response exceeds 8 MiB"));
        let end;
        while ((end = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, end);
          buffer = buffer.slice(end + 1);
          try {
            const message = JSON.parse(line);
            if (message.id !== id) continue;
            if (message.error || message.ok === false)
              return finish(
                new Error(
                  `Herdr ${method}: ${message.error?.message || JSON.stringify(message.error)}`,
                ),
              );
            return finish(null, message.result);
          } catch (error) {
            return finish(error);
          }
        }
      });
      connection.once("end", () =>
        finish(new Error(`Herdr disconnected during ${method}; reconcile before retrying`)),
      );
    });
}
