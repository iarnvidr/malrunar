package migrations

import (
 "github.com/pocketbase/pocketbase/core"
 m "github.com/pocketbase/pocketbase/migrations"
)

func init() {
 m.Register(func(app core.App) error {
  return collection(app, "calendar_events", map[string]string{"uevent": "`event_id`", "itime": "`start_at`, `end_at`"},
   text("event_id", 2000, true), number("start_at", false), number("end_at", false),
   text("generation", 100, false), jsonField("event", 3000000),
  )
 }, func(app core.App) error {
  c, err := app.FindCollectionByNameOrId("calendar_events")
  if err != nil { return err }
  return app.Delete(c)
 })
}
