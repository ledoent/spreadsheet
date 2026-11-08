/**
 * Transport for the o-spreadsheet collaborative session.
 *
 * Revisions are persisted and acked; presence (CLIENT_*) is fire-and-forget,
 * since o-spreadsheet emits one on every selection change and each costs a
 * bus_bus row and a commit here.
 */

/** How often, at most, to tell the server where our cursor is. */
export const MOVE_THROTTLE_MS = 500;
/** A dropped CLIENT_JOINED leaves us blind to existing peers, so retry once. */
export const JOIN_RETRY_MS = 1000;

export class SpreadsheetTransportService {
    /**
     * @param {Object} orm the orm service
     * @param {Object} busService the bus service
     * @param {String} model res_model of the spreadsheet record
     * @param {Number} resId
     * @param {Object} [options]
     * @param {function(): number} [options.getClientCount] connected clients,
     *      us included. Defaults high, so a surprise costs a redundant ping
     *      rather than a missing cursor.
     * @param {String|null} [options.accessToken]
     */
    constructor(orm, busService, model, resId, options = {}) {
        // Only presence is silenced; a revision is a real write and a stall
        // should stay visible.
        this.orm = orm;
        this.silentOrm = orm.silent;
        this.busService = busService;
        this.model = model;
        this.resId = resId;
        this.accessToken = options.accessToken ?? null;
        this.getClientCount = options.getClientCount ?? (() => 2);

        this.channel = `spreadsheet_oca;${this.model};${this.resId}`;
        this.busService.addChannel(this.channel);
        this.unsubscribe = this.busService.subscribe("spreadsheet_oca", (payload) => {
            if (payload.id === this.resId) {
                this._dispatch(payload);
            }
        });

        /** @type {Map<string, Function>} clientId -> callback */
        this.callbacks = new Map();
        /** Messages that arrived before anyone registered a callback. */
        this.buffer = [];

        this.moveCooldown = null;
        this.pendingMove = null;
    }

    /**
     * @param {Object} message
     * @returns {Promise} must stay thenable: Session.move() chains .then() on
     *      it and ReadonlyTransportFilter awaits it.
     */
    async sendMessage(message) {
        if (message.type === "CLIENT_MOVED") {
            this._queueMove(message);
            return;
        }
        if (message.type === "CLIENT_JOINED") {
            // Never suppressed: peers learn we exist only from this, and we
            // learn about them only from their reply.
            this._sendPresence(message, true);
            return;
        }
        if (message.type === "CLIENT_LEFT") {
            // Awaited: there is no presence timeout, so losing it leaves a
            // ghost cursor.
            await this._sendPresence(message, false);
            return;
        }
        // The local echo is the self-ack that clears waitingAck.
        const isAccepted = await this.orm.call(this.model, "send_spreadsheet_message", [
            [this.resId],
            message,
            this.accessToken,
        ]);
        if (isAccepted) {
            this._dispatch(message);
        }
    }

    onNewMessage(id, callback) {
        this.callbacks.set(id, callback);
        const buffered = this.buffer;
        this.buffer = [];
        for (const message of buffered) {
            callback(message);
        }
    }

    leave(id) {
        this.callbacks.delete(id);
        if (this.callbacks.size) {
            return;
        }
        this._cancelPendingMove();
        this.unsubscribe?.();
        this.unsubscribe = null;
        this.busService.deleteChannel(this.channel);
    }

    /** Swallows errors: a lost ping costs a stale cursor, nothing more. */
    _sendPresence(message, retryOnce) {
        const sent = this.silentOrm
            .call(this.model, "send_spreadsheet_message", [
                [this.resId],
                message,
                this.accessToken,
            ])
            .catch(() => {
                if (retryOnce) {
                    setTimeout(() => this._sendPresence(message, false), JOIN_RETRY_MS);
                }
            });
        return sent;
    }

    /** Leading + trailing throttle, coalescing to the latest position. */
    _queueMove(message) {
        if (this.getClientCount() <= 1) {
            // Not a lost update: move() records the position locally first,
            // and onClientJoined re-broadcasts it when someone arrives.
            return;
        }
        if (this.moveCooldown) {
            this.pendingMove = message;
            return;
        }
        this._sendPresence(message, false);
        this.moveCooldown = setTimeout(() => {
            this.moveCooldown = null;
            const pending = this.pendingMove;
            this.pendingMove = null;
            if (pending) {
                this._queueMove(pending);
            }
        }, MOVE_THROTTLE_MS);
    }

    _cancelPendingMove() {
        if (this.moveCooldown) {
            clearTimeout(this.moveCooldown);
            this.moveCooldown = null;
        }
        this.pendingMove = null;
    }

    _dispatch(message) {
        if (!this.callbacks.size) {
            this.buffer.push(message);
            return;
        }
        for (const callback of this.callbacks.values()) {
            callback(message);
        }
    }
}
