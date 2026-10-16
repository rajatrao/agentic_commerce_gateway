-- CreateTable
CREATE TABLE "ServiceTokenJti" (
    "jti" TEXT NOT NULL,
    "expiresAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ServiceTokenJti_pkey" PRIMARY KEY ("jti")
);

-- CreateIndex
CREATE INDEX "ServiceTokenJti_expiresAt_idx" ON "ServiceTokenJti"("expiresAt");
