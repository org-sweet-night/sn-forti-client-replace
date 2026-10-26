import AppKit
import Foundation

enum EnergyTests {
    private static func drain(until condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(1)
        while !condition(), Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.001))
        }
        precondition(condition(), "status completion did not settle")
    }

    static func run() {
        let connected = StatusReply(state: .connected, reachable: true, autostart: true,
                                    message: "connected", profileID: "primary")
        let stopped = StatusReply(state: .stopped, reachable: false, autostart: false,
                                  message: "stopped", profileID: nil)
        let app = App()
        app.testMode = true
        var callbacks: [(Result<StatusReply, Error>) -> Void] = []
        app.statusRequest = { callbacks.append($0) }

        app.refreshStatus()
        app.refreshStatus()
        precondition(callbacks.count == 1 && app.statusRequestInFlight)
        app.refreshStatus(force: true)
        app.refreshStatus(force: true)
        precondition(callbacks.count == 1 && app.statusRefreshPending)
        callbacks[0](.success(connected))
        drain { callbacks.count == 2 }
        callbacks[1](.success(stopped))
        drain { !app.statusRequestInFlight }
        precondition(callbacks.count == 2 && !app.statusRefreshPending && app.lastStatus == stopped)

        // An operation invalidates every reply from the preceding generation.
        for stale in [Result<StatusReply, Error>.success(connected), .failure(AppError.timeout)] {
            let count = callbacks.count
            app.refreshStatus()
            precondition(callbacks.count == count + 1)
            app.statusGeneration += 1
            app.lastStatus = stopped
            callbacks[count](stale)
            drain { !app.statusRequestInFlight }
            precondition(app.lastStatus == stopped)
        }

        var count = callbacks.count
        app.refreshStatus()
        callbacks[count](.failure(AppError.timeout))
        drain { !app.statusRequestInFlight }
        precondition(app.lastStatus?.state == .error)
        count = callbacks.count
        app.refreshStatus()
        precondition(callbacks.count == count + 1)
        callbacks[count](.success(stopped))
        drain { !app.statusRequestInFlight }

        count = callbacks.count
        app.busy = true
        app.refreshStatus(force: true)
        app.refreshStatus(force: true)
        precondition(callbacks.count == count && !app.statusRequestInFlight)
        app.busy = false
        app.refreshStatus(force: true)
        precondition(callbacks.count == count + 1)
        callbacks[count](.success(stopped))
        drain { !app.statusRequestInFlight }
        precondition(callbacks.count == count + 1 && !app.statusRefreshPending)

        precondition(App.pollInterval(status: connected, visible: true) == 60)
        precondition(App.pollInterval(status: connected, visible: false) == 300)
        let transitioning = StatusReply(state: .reconnecting, reachable: false, autostart: true,
                                        message: "reconnecting", profileID: "primary")
        precondition(App.pollInterval(status: transitioning, visible: true) == 5)
        precondition(App.pollInterval(status: transitioning, visible: false) == 15)
        precondition(connected == StatusReply(state: .connected, reachable: true, autostart: true,
                                             message: "connected", profileID: "primary"))
        precondition(connected != StatusReply(state: .connected, reachable: false, autostart: true,
                                             message: "connected", profileID: "primary"))
        precondition(connected != StatusReply(state: .connected, reachable: true, autostart: true,
                                             message: "changed", profileID: "primary"))
        precondition(connected != StatusReply(state: .connected, reachable: true, autostart: true,
                                             message: "connected", profileID: "other"))
        precondition(App.statusIcon(connected: true) != nil && App.statusIcon(connected: false) != nil)
        precondition(App.statusIcon(connected: true) === App.statusIcon(connected: true))
        precondition(App.statusIcon(connected: false) === App.statusIcon(connected: false))
        print("energy self-test: OK")
    }
}
