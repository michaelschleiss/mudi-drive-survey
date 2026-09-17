// Feed the laptop's own position (macOS Location Services: Wi-Fi/cell-based, no GPS chip) to the survey tools.
// Usage:  swift laptop_location.swift [port ...]     default ports 8765 8766
// macOS will ask once for Location permission for the terminal app; allow it.
import Foundation
import CoreLocation

let ports: [Int] = CommandLine.arguments.dropFirst().compactMap { Int($0) }.isEmpty ? [8765, 8766] : CommandLine.arguments.dropFirst().compactMap { Int($0) }

final class Feeder: NSObject, CLLocationManagerDelegate {
    let lm = CLLocationManager()
    var last: CLLocation?
    override init() {
        super.init()
        lm.delegate = self
        lm.desiredAccuracy = kCLLocationAccuracyBest
        lm.distanceFilter = kCLDistanceFilterNone
        lm.requestAlwaysAuthorization()
        lm.startUpdatingLocation()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.post() }
    }
    func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        if let l = locs.last { last = l; post() }
    }
    func locationManager(_ m: CLLocationManager, didFailWithError e: Error) {
        FileHandle.standardError.write("location error: \(e.localizedDescription)\n".data(using: .utf8)!)
    }
    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        print("authorization: \(m.authorizationStatus.rawValue) (3/4 = allowed)")
    }
    func post() {
        guard let l = last else { return }
        let speed = l.speed >= 0 ? l.speed * 3.6 : -1
        let body = "{\"lat\":\(l.coordinate.latitude),\"lon\":\(l.coordinate.longitude),\"speed_kmh\":\(speed >= 0 ? String(speed) : "null"),\"acc_m\":\(l.horizontalAccuracy)}"
        print(String(format: "%@  %.5f, %.5f  ±%.0f m  %@", ISO8601DateFormatter().string(from: l.timestamp), l.coordinate.latitude, l.coordinate.longitude, l.horizontalAccuracy, speed >= 0 ? String(format: "%.0f km/h", speed) : ""))
        for p in ports {
            var r = URLRequest(url: URL(string: "http://127.0.0.1:\(p)/api/pos")!)
            r.httpMethod = "POST"; r.httpBody = body.data(using: .utf8); r.timeoutInterval = 2
            URLSession.shared.dataTask(with: r).resume()
        }
    }
}
let f = Feeder()
RunLoop.main.run()
