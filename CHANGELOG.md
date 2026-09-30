# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-09-30

### Added
- `get_grades` now returns written (text) evaluations as `text_grades`, filtered
  by `subject` and `since` like numeric marks and honouring `school_year`/`term`.
  Schools without written evaluations get an empty list.

### Changed
- Upgraded `edupage-api` to 0.13.0 (pin is now `>=0.13.0,<0.14`). This brings the
  text-grade methods and a fix for modern EduPage two-factor login, plus the
  0.12.7 fixes for timetable event names, localized message bodies and new
  timeline event types.

## [0.1.0]

- Initial release.
