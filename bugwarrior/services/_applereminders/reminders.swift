// Public EventKit APIs only. This executable contains no reminder write calls.
import Foundation
import EventKit
import Darwin

struct ReaderError: Error {
    let message: String
    init(_ message: String) { self.message = message }
}

func nullable(_ value: Any?) -> Any { value ?? NSNull() }

func timestamp(_ date: Date) -> String {
    ISO8601DateFormatter().string(from: date)
}

func dateMetadata(_ components: DateComponents?, endOfDay: Bool) throws -> Any {
    guard let components = components else { return NSNull() }
    guard let year = components.year, let month = components.month,
          let day = components.day else {
        throw ReaderError("A reminder has incomplete date components")
    }
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = components.timeZone ?? TimeZone.current
    var wallTime = DateComponents()
    wallTime.year = year
    wallTime.month = month
    wallTime.day = day
    wallTime.hour = components.hour ?? 0
    wallTime.minute = components.minute ?? 0
    wallTime.second = components.second ?? 0
    guard var date = calendar.date(from: wallTime) else {
        throw ReaderError("A reminder has an invalid date")
    }
    let dateOnly = components.hour == nil && components.minute == nil && components.second == nil
    if dateOnly && endOfDay {
        // Calendar arithmetic matters: DST days are not always 24 hours long.
        guard let nextDay = calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: date)) else {
            throw ReaderError("Could not resolve an all-day deadline")
        }
        date = nextDay.addingTimeInterval(-1)
    }
    return [
        "value": timestamp(date),
        "date_only": dateOnly,
        "time_zone": nullable(components.timeZone?.identifier),
        "local_date": String(format: "%04d-%02d-%02d", year, month, day),
    ] as [String: Any]
}

func recurrenceMetadata(_ rule: EKRecurrenceRule) -> [String: Any] {
    var result: [String: Any] = [
        "calendar": rule.calendarIdentifier,
        "frequency": rule.frequency.rawValue,
        "interval": rule.interval,
        "first_day_of_week": rule.firstDayOfTheWeek,
        "weekdays": (rule.daysOfTheWeek ?? []).map {
            ["day": $0.dayOfTheWeek.rawValue, "week": $0.weekNumber]
        },
        "month_days": rule.daysOfTheMonth ?? [],
        "year_days": rule.daysOfTheYear ?? [],
        "year_weeks": rule.weeksOfTheYear ?? [],
        "months": rule.monthsOfTheYear ?? [],
        "positions": rule.setPositions ?? [],
    ]
    if let end = rule.recurrenceEnd {
        result["end_date"] = nullable(end.endDate.map(timestamp))
        result["occurrence_count"] = end.occurrenceCount
    }
    return result
}

func alarmMetadata(_ alarm: EKAlarm) -> [String: Any] {
    var result: [String: Any] = [
        "absolute_date": nullable(alarm.absoluteDate.map(timestamp)),
        "relative_offset": alarm.relativeOffset,
        "proximity": alarm.proximity.rawValue,
        "type": alarm.type.rawValue,
        "email_address": nullable(alarm.emailAddress),
        "sound_name": nullable(alarm.soundName),
    ]
    if let location = alarm.structuredLocation {
        var details: [String: Any] = ["title": nullable(location.title), "radius": location.radius]
        if let point = location.geoLocation {
            details["latitude"] = point.coordinate.latitude
            details["longitude"] = point.coordinate.longitude
        }
        result["location"] = details
    }
    return result
}

func calendarMetadata(_ calendar: EKCalendar) -> [String: Any] {
    [
        "id": calendar.calendarIdentifier,
        "title": calendar.title,
        "source_id": calendar.source.sourceIdentifier,
        "source_title": calendar.source.title,
    ]
}

func reminderMetadata(_ reminder: EKReminder) throws -> [String: Any] {
    guard let calendar = reminder.calendar else {
        throw ReaderError("A reminder belongs to an unavailable calendar; snapshot aborted")
    }
    let external = reminder.calendarItemExternalIdentifier
    let hasExternal = !(external ?? "").isEmpty
    return [
        "id": hasExternal ? "external:\(external!)" : "local:\(reminder.calendarItemIdentifier)",
        "id_kind": hasExternal ? "external" : "local",
        "local_id": reminder.calendarItemIdentifier,
        "calendar_id": calendar.calendarIdentifier,
        "title": reminder.title ?? "",
        "notes": nullable(reminder.notes),
        "url": nullable(reminder.url?.absoluteString),
        "created": nullable(reminder.creationDate.map(timestamp)),
        "modified": nullable(reminder.lastModifiedDate.map(timestamp)),
        "completed": reminder.isCompleted,
        "completed_at": nullable(reminder.completionDate.map(timestamp)),
        "due": try dateMetadata(reminder.dueDateComponents, endOfDay: true),
        "start": try dateMetadata(reminder.startDateComponents, endOfDay: false),
        "priority": reminder.priority,
        "recurrence": (reminder.recurrenceRules ?? []).map(recurrenceMetadata),
        "alarms": (reminder.alarms ?? []).map(alarmMetadata),
    ]
}

func writeJSON(_ object: Any) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

// MARK: Command entry point
let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count == 1, ["authorize", "lists", "snapshot"].contains(arguments[0]) else {
    fail("Usage: bugwarrior-reminders authorize|lists|snapshot")
}
let command = arguments[0]
let store = EKEventStore()

func readStore() {
    if command == "authorize" {
        print("Reminders access granted. This helper only reads reminders.")
        exit(0)
    }
    let calendars = store.calendars(for: .reminder)
    if command == "lists" {
        do { try writeJSON(calendars.map(calendarMetadata)); exit(0) }
        catch { fail("Could not encode reminder lists") }
    }
    store.fetchReminders(matching: store.predicateForReminders(in: nil)) { reminders in
        guard let reminders = reminders else { fail("EventKit did not return a snapshot", code: 4) }
        guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess else {
            fail("Reminders permission was revoked during collection", code: 3)
        }
        do {
            // Serialize everything before writing stdout, so an error never
            // looks like a valid partial collection or an empty snapshot.
            try writeJSON([
                "version": 1,
                "calendars": calendars.map(calendarMetadata),
                "reminders": try reminders.map(reminderMetadata),
            ])
            exit(0)
        } catch let error as ReaderError {
            fail(error.message, code: 4)
        } catch {
            fail("Could not encode the reminder snapshot", code: 4)
        }
    }
}

let status = EKEventStore.authorizationStatus(for: .reminder)
if status == .fullAccess {
    readStore()
} else if status == .notDetermined && command == "authorize" {
    store.requestFullAccessToReminders { granted, _ in
        if granted { readStore() }
        else { fail("Reminders access denied; check System Settings > Privacy & Security > Reminders", code: 3) }
    }
} else {
    fail("Reminders access is unavailable. Run this helper's authorize command from the importing app or terminal, and check System Settings > Privacy & Security > Reminders", code: 3)
}
DispatchQueue.global().asyncAfter(deadline: .now() + (command == "authorize" ? 120 : 45)) {
    fail("Reminders access or fetch timed out", code: 5)
}
dispatchMain()
