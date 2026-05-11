// 경로: src/routes/your-feature/index.ts
import { Router, type IRouter, type Request, type Response } from "express";
import { callUpstream, UpstreamError, type CallUpstreamOptions } from "../../lib/upstreamClient";
import { getVisitorToken } from "../../middlewares/session";
import { logger } from "../../lib/logger";

const router: IRouter = Router();

async function forward(
  req: Request,
  res: Response,
  options: Omit<CallUpstreamOptions, "visitorToken">,
): Promise<void> {
  try {
    const upstream = await callUpstream({ ...options, visitorToken: getVisitorToken(req) });
    if (upstream.status === 204) { res.status(204).end(); return; }
    res.status(upstream.status).json(upstream.body);
  } catch (err) {
    if (err instanceof UpstreamError) {
      logger.error({ err, path: options.path }, "Upstream proxy error");
      res.status(err.status >= 400 ? err.status : 502).json({ message: err.message });
      return;
    }
    logger.error({ err, path: options.path }, "Proxy error");
    res.status(502).json({ message: "Upstream request failed" });
  }
}

// Replace these with your actual endpoints:

router.get("/list", (req, res) =>
  forward(req, res, {
    method: "GET",
    path: "/your-feature/list",
    query: {
      offset: req.query["offset"] ? String(req.query["offset"]) : undefined,
      limit: req.query["limit"] ? String(req.query["limit"]) : undefined,
    },
  })
);

router.get("/:id", (req, res) =>
  forward(req, res, {
    method: "GET",
    path: `/your-feature/${encodeURIComponent(req.params["id"] as string)}`,
  })
);

router.post("/:id/action", (req, res) =>
  forward(req, res, {
    method: "POST",
    path: `/your-feature/${encodeURIComponent(req.params["id"] as string)}/action`,
    body: req.body,
  })
);

export default router;
