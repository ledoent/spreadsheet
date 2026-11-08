import {
    JOIN_RETRY_MS,
    MOVE_THROTTLE_MS,
    SpreadsheetTransportService,
} from "@spreadsheet_oca/spreadsheet/bundle/spreadsheet_transport_service.esm";
import {describe, expect, test} from "@odoo/hoot";
import {advanceTime} from "@odoo/hoot-mock";

describe.current.tags("headless");

/**
 * Minimal doubles. The point of these tests is the branching and the timers,
 * so neither the bus nor the ORM needs to be real -- they only need to record
 * what the transport asked of them.
 */
function makeTransport({clientCount = 2, failFirstCall = false} = {}) {
    const calls = [];
    let callIndex = 0;
    const call = (model, method, args) => {
        calls.push({silent: false, message: args[1]});
        return Promise.resolve(true);
    };
    const orm = {
        call,
        silent: {
            call: (model, method, args) => {
                calls.push({silent: true, message: args[1]});
                callIndex++;
                if (failFirstCall && callIndex === 1) {
                    return Promise.reject(new Error("boom"));
                }
                return Promise.resolve(true);
            },
        },
    };
    const bus = {
        channels: [],
        deleted: [],
        unsubscribed: 0,
        addChannel(channel) {
            this.channels.push(channel);
        },
        deleteChannel(channel) {
            this.deleted.push(channel);
        },
        subscribe() {
            return () => {
                bus.unsubscribed++;
            };
        },
    };
    const transport = new SpreadsheetTransportService(
        orm,
        bus,
        "spreadsheet.spreadsheet",
        1,
        {
            getClientCount: () => clientCount,
        }
    );
    return {transport, calls, bus};
}

const move = (col) => ({
    type: "CLIENT_MOVED",
    client: {id: "c1", position: {col, row: 0}},
});

describe("message type branching", () => {
    test("a revision is awaited, non-silent, and echoed back as its own ack", async () => {
        const {transport, calls} = makeTransport();
        const received = [];
        transport.onNewMessage("c1", (m) => received.push(m));

        await transport.sendMessage({type: "REMOTE_REVISION", nextRevisionId: "r1"});

        expect(calls).toHaveLength(1);
        expect(calls[0].silent).toBe(false);
        // The self-dispatch is what clears waitingAck in o-spreadsheet.
        expect(received).toHaveLength(1);
        expect(received[0].type).toBe("REMOTE_REVISION");
    });

    test("presence is silent and is never echoed back locally", async () => {
        const {transport, calls} = makeTransport();
        const received = [];
        transport.onNewMessage("c1", (m) => received.push(m));

        await transport.sendMessage(move(1));

        expect(calls).toHaveLength(1);
        expect(calls[0].silent).toBe(true);
        expect(received).toHaveLength(0);
    });

    test("CLIENT_LEFT stays awaited so peers do not keep a ghost cursor", async () => {
        const {transport, calls} = makeTransport();
        let settled = false;
        await transport.sendMessage({type: "CLIENT_LEFT", clientId: "c1"}).then(() => {
            settled = true;
        });
        expect(settled).toBe(true);
        expect(calls).toHaveLength(1);
    });

    test("sendMessage always returns a thenable", async () => {
        const {transport} = makeTransport();
        // Session.move() chains .then() on this and ReadonlyTransportFilter
        // awaits it, so returning undefined would break collaboration.
        expect(typeof transport.sendMessage(move(1)).then).toBe("function");
    });
});

describe("suppress while alone", () => {
    test("no cursor broadcast when we are the only client", async () => {
        const {transport, calls} = makeTransport({clientCount: 1});
        await transport.sendMessage(move(1));
        await transport.sendMessage(move(2));
        expect(calls).toHaveLength(0);
    });

    test("joining and leaving are still sent when alone", async () => {
        const {transport, calls} = makeTransport({clientCount: 1});
        await transport.sendMessage({type: "CLIENT_JOINED", client: {id: "c1"}});
        await transport.sendMessage({type: "CLIENT_LEFT", clientId: "c1"});
        expect(calls).toHaveLength(2);
    });

    test("broadcasting resumes once a second client is connected", async () => {
        const {transport, calls} = makeTransport({clientCount: 2});
        await transport.sendMessage(move(1));
        expect(calls).toHaveLength(1);
    });
});

describe("CLIENT_MOVED throttle", () => {
    test("leading edge sends immediately, the rest coalesce to the last position", async () => {
        const {transport, calls} = makeTransport();

        for (const col of [1, 2, 3, 4, 5]) {
            transport.sendMessage(move(col));
        }
        // Leading edge only.
        expect(calls).toHaveLength(1);
        expect(calls[0].message.client.position.col).toBe(1);

        await advanceTime(MOVE_THROTTLE_MS);
        // Trailing edge carries the newest position, not the queued ones.
        expect(calls).toHaveLength(2);
        expect(calls[1].message.client.position.col).toBe(5);

        await advanceTime(MOVE_THROTTLE_MS);
        // Nothing left pending.
        expect(calls).toHaveLength(2);
    });

    test("teardown mid-window drops the pending move", async () => {
        const {transport, calls} = makeTransport();
        const received = [];
        transport.onNewMessage("c1", (m) => received.push(m));

        transport.sendMessage(move(1));
        transport.sendMessage(move(2));
        expect(calls).toHaveLength(1);

        transport.leave("c1");
        await advanceTime(MOVE_THROTTLE_MS * 2);
        // A departing client must not keep announcing where it is.
        expect(calls).toHaveLength(1);
    });
});

describe("CLIENT_JOINED retry", () => {
    test("retries once, then gives up", async () => {
        const {transport, calls} = makeTransport({failFirstCall: true});

        await transport.sendMessage({type: "CLIENT_JOINED", client: {id: "c1"}});
        expect(calls).toHaveLength(1);

        await advanceTime(JOIN_RETRY_MS);
        expect(calls).toHaveLength(2);

        await advanceTime(JOIN_RETRY_MS * 3);
        // One retry, not a loop.
        expect(calls).toHaveLength(2);
    });
});

describe("teardown", () => {
    test("the last client out unsubscribes and drops the channel", () => {
        const {transport, bus} = makeTransport();
        const received = [];
        transport.onNewMessage("c1", (m) => received.push(m));
        transport.onNewMessage("c2", (m) => received.push(m));

        transport.leave("c1");
        expect(bus.deleted).toHaveLength(0);
        expect(bus.unsubscribed).toBe(0);

        transport.leave("c2");
        expect(bus.deleted).toEqual([transport.channel]);
        expect(bus.unsubscribed).toBe(1);
    });

    test("messages arriving before a callback is registered are not lost", () => {
        const {transport} = makeTransport();
        transport._dispatch({type: "REMOTE_REVISION", nextRevisionId: "r1"});

        const received = [];
        transport.onNewMessage("c1", (m) => received.push(m));
        expect(received).toHaveLength(1);
    });
});
