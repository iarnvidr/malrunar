// Command malrunar-teams-pocketbase is the PocketBase that keeps the local copy of Teams chats
// and calendar of the teams-service component. It has no web UI of its own and is meant to listen
// on loopback only; just the Teams service (as superuser) talks to it.
package main

import (
	"log"

	"github.com/pocketbase/pocketbase"
	"github.com/pocketbase/pocketbase/plugins/migratecmd"

	_ "github.com/iarnvidr/malrunar/teams-service/pocketbase/migrations"
)

func main() {
	app := pocketbase.New()
	// Schema changes are reviewed Go migrations, never files written from dashboard edits.
	migratecmd.MustRegister(app, app.RootCmd, migratecmd.Config{
		TemplateLang: migratecmd.TemplateLangGo,
		Automigrate:  false,
		Dir:          "migrations",
	})
	if err := app.Start(); err != nil {
		log.Fatal(err)
	}
}
