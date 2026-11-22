import XCTest

final class RoomUITests: XCTestCase {
    private func startAtPairing(isolatePairing: Bool = false) -> XCUIApplication {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
        let app = XCUIApplication()
        app.launchArguments = ["-roomOrigin", ""]
        if isolatePairing { app.launchArguments.append("--isolated-pairing-test") }
        app.launch()
        return app
    }

    func testEmptyAddress() {
        let app = startAtPairing()
        let field = app.textFields["Server URL"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        app.buttons["Connect"].tap()
        XCTAssertTrue(app.staticTexts["Enter a full server URL starting with http:// or https://."].exists)
    }

    func testServerURLLeadsToPairing() throws {
        guard let url = ProcessInfo.processInfo.environment["LISTENING_FIXTURE_URL"],
              var parts = URLComponents(string: url) else {
            throw XCTSkip("Provide a disposable room using TEST_RUNNER_LISTENING_FIXTURE_URL.")
        }
        parts.query = nil
        parts.path = "/"
        let server = try XCTUnwrap(parts.url)
        let app = startAtPairing(isolatePairing: true)
        let field = app.textFields["Server URL"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText(server.absoluteString + "\n")
        let prompt = XCUIApplication(bundleIdentifier: "com.apple.springboard").alerts.buttons["Allow"]
        if prompt.waitForExistence(timeout: 5) { prompt.tap() }

        let invitation = app.secureTextFields["Pairing link"]
        XCTAssertTrue(invitation.waitForExistence(timeout: 15))
        app.buttons["Change server URL"].tap()
        XCTAssertEqual(field.value as? String, server.absoluteString)
        app.buttons["Connect"].tap()
        XCTAssertTrue(invitation.waitForExistence(timeout: 15))
        invitation.tap()
        invitation.typeText(url + "\n")
        let playlist = app.webViews.buttons.containing(NSPredicate(format: "label CONTAINS[c] %@", "evening")).firstMatch
        XCTAssertTrue(playlist.waitForExistence(timeout: 30))
    }

    func testPairPlayAndReconnect() throws {
        guard let url = ProcessInfo.processInfo.environment["LISTENING_FIXTURE_URL"] else {
            throw XCTSkip("Provide a disposable room using TEST_RUNNER_LISTENING_FIXTURE_URL.")
        }
        let app = startAtPairing()
        let field = app.textFields["Server URL"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText(url + "\n")
        let prompt = XCUIApplication(bundleIdentifier: "com.apple.springboard").alerts.buttons["Allow"]
        if prompt.waitForExistence(timeout: 5) { prompt.tap() }
        let playlist = app.webViews.buttons.containing(NSPredicate(format: "label CONTAINS[c] %@", "evening")).firstMatch
        XCTAssertTrue(playlist.waitForExistence(timeout: 45))
        playlist.tap()
        let play = app.webViews.buttons["Play playlist"]
        XCTAssertTrue(play.waitForExistence(timeout: 10))
        play.tap()
        XCTAssertTrue(app.webViews.buttons["Pause"].firstMatch.waitForExistence(timeout: 15))
        XCUIDevice.shared.orientation = .landscapeLeft
        defer { XCUIDevice.shared.orientation = .portrait }
        XCTAssertTrue(play.waitForExistence(timeout: 10))
        let landscape = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            app.frame.width > app.frame.height && play.isHittable
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [landscape], timeout: 10), .completed)
        let pause = app.webViews.buttons["Pause"].firstMatch
        pause.tap()
        let resume = app.webViews.buttons["Play"].firstMatch
        XCTAssertTrue(resume.waitForExistence(timeout: 10))
        play.tap()
        XCTAssertTrue(pause.waitForExistence(timeout: 10))
        let picture = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        picture.name = "Playing a room playlist on iPad"
        picture.lifetime = .keepAlways
        add(picture)
        app.terminate()
        app.launchArguments = []
        app.launch()
        XCTAssertTrue(app.webViews.firstMatch.waitForExistence(timeout: 15))
        XCTAssertTrue(playlist.waitForExistence(timeout: 30) || app.webViews.buttons["Play playlist"].exists)
    }

    @MainActor func testBackgroundPlaybackAdvancesTheQueue() async throws {
        guard let address = ProcessInfo.processInfo.environment["LISTENING_FIXTURE_URL"],
              var endpoints = URLComponents(string: address) else {
            throw XCTSkip("Provide the disposable UI fixture using TEST_RUNNER_LISTENING_FIXTURE_URL.")
        }
        endpoints.host = "127.0.0.1"
        endpoints.query = nil
        endpoints.path = "/api/session"
        let endpoint = try XCTUnwrap(endpoints.url)

        let app = startAtPairing()
        let field = app.textFields["Server URL"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap()
        field.typeText(address + "\n")
        let permission = XCUIApplication(bundleIdentifier: "com.apple.springboard").alerts.buttons["Allow"]
        if permission.waitForExistence(timeout: 5) { permission.tap() }
        let play = app.webViews.buttons["Play Atlas Air"].firstMatch
        XCTAssertTrue(play.waitForExistence(timeout: 30))
        play.tap()
        let pause = app.webViews.buttons["Pause"].firstMatch
        XCTAssertTrue(pause.waitForExistence(timeout: 15))

        func seek(to seconds: Int) async throws {
            var request = URLRequest(url: endpoint)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: ["op": "seek", "position": seconds])
            let (_, response) = try await URLSession.shared.data(for: request)
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        }

        func reached(_ title: String) async throws -> Bool {
            for _ in 0..<42 {
                let (data, _) = try await URLSession.shared.data(from: endpoint)
                let snapshot = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                let current = snapshot?["current"] as? [String: Any]
                if current?["title"] as? String == title { return true }
                try await Task.sleep(for: .milliseconds(500))
            }
            return false
        }

        try await seek(to: 59)
        XCUIDevice.shared.press(.home)
        let firstAdvance = try await reached("Asleep")
        XCTAssertTrue(firstAdvance, "The queue should advance while the app is in the background.")
        try await seek(to: 44)
        let secondAdvance = try await reached("Paradise Circus")
        XCTAssertTrue(secondAdvance, "The next song should also play through in the background.")
        XCTAssertNotEqual(app.state, .runningForeground)
        app.activate()
        XCTAssertTrue(pause.waitForExistence(timeout: 10))
    }
}
