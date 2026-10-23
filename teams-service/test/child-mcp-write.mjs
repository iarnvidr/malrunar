// Stands for the MCP: writes a session file through the library, unguarded.
import { writeAsMcp } from './fixture.mjs';
await writeAsMcp(process.env.MALRUNAR_TEAMS_SERVICE_MCP_DIST, Math.floor(Date.now() / 1000) + Number(process.argv[2]), process.argv[3] || 'old-refresh');
