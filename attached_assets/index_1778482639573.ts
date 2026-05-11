import { Router, type IRouter } from "express";
import healthRouter from "./health";
import authRouter from "./auth";
// import yourFeatureRouter from "./your-feature";

const router: IRouter = Router();

router.use(healthRouter);
router.use("/auth", authRouter);
// router.use("/your-feature", yourFeatureRouter);

export default router;
