const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("LaunchToken", function () {
  it("mints the entire supply to the initial owner", async function () {
    const [owner] = await ethers.getSigners();
    const Token = await ethers.getContractFactory("LaunchToken");
    const token = await Token.deploy("Test Token", "TEST", 1_000_000n * 10n ** 18n, owner.address, "");
    await token.waitForDeployment();
    expect(await token.balanceOf(owner.address)).to.equal(1_000_000n * 10n ** 18n);
    expect(await token.totalSupply()).to.equal(1_000_000n * 10n ** 18n);
  });

  it("has no mint function at all", async function () {
    const Token = await ethers.getContractFactory("LaunchToken");
    expect(Token.interface.fragments.some((f) => f.name === "mint")).to.equal(false);
  });

  it("allows the owner to renounce ownership permanently", async function () {
    const [owner] = await ethers.getSigners();
    const Token = await ethers.getContractFactory("LaunchToken");
    const token = await Token.deploy("Test Token", "TEST", 1000n, owner.address, "");
    await token.waitForDeployment();
    await token.renounceOwnership();
    expect(await token.owner()).to.equal(ethers.ZeroAddress);
  });
});

describe("LaunchTokenFactory", function () {
  it("deploys a token owned by the caller with full supply", async function () {
    const [creator, treasury] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("LaunchTokenFactory");
    const factory = await Factory.deploy(treasury.address, 0);
    await factory.waitForDeployment();
    const tx = await factory.launchToken("Alpha", "ALP", 1000n * 10n ** 18n, "https://ferzaneco.com");
    const rc = await tx.wait();
    const ev = rc.logs.find((l) => l.fragment && l.fragment.name === "TokenLaunched");
    const tokenAddr = ev.args.token;
    const Token = await ethers.getContractFactory("LaunchToken");
    const token = Token.attach(tokenAddr);
    expect(await token.owner()).to.equal(creator.address);
    expect(await token.balanceOf(creator.address)).to.equal(1000n * 10n ** 18n);
  });
});

describe("BondingCurve", function () {
  async function deployCurve() {
    const [creator, trader, treasury] = await ethers.getSigners();
    const Router = await ethers.getContractFactory("MockUniswapV2Router");
    const router = await Router.deploy();
    await router.waitForDeployment();
    const Factory = await ethers.getContractFactory("BondingCurveFactory");
    const factory = await Factory.deploy(await router.getAddress(), treasury.address);
    await factory.waitForDeployment();
    const supply = 1_000_000n * 10n ** 18n;
    const virtualEth = ethers.parseEther("1");
    const virtualToken = 800_000n * 10n ** 18n;
    const threshold = ethers.parseEther("2");
    const tx = await factory.connect(creator).launch(
      "CurveTok",
      "CRV",
      supply,
      threshold,
      virtualEth,
      virtualToken
    );
    const rc = await tx.wait();
    const ev = rc.logs.find((l) => l.fragment && l.fragment.name === "CurveLaunched");
    const curveAddr = ev.args.curve;
    const tokenAddr = ev.args.token;
    const Curve = await ethers.getContractFactory("BondingCurve");
    const Token = await ethers.getContractFactory("LaunchToken");
    return {
      creator,
      trader,
      treasury,
      router,
      curve: Curve.attach(curveAddr),
      token: Token.attach(tokenAddr),
      supply,
      threshold,
    };
  }

  it("buy() gives fewer tokens for the same ETH as the curve fills (price rises)", async function () {
    const { curve, trader } = await deployCurve();
    const one = ethers.parseEther("0.2");
    const out1 = await curve.quoteBuy(one);
    await curve.connect(trader).buy(0, { value: one });
    const out2 = await curve.quoteBuy(one);
    expect(out2).to.be.lt(out1);
  });

  it("rejects a buy below minTokensOut (slippage protection)", async function () {
    const { curve, trader } = await deployCurve();
    const huge = 10n ** 30n;
    await expect(curve.connect(trader).buy(huge, { value: ethers.parseEther("0.1") }))
      .to.be.revertedWith("slippage: price moved against you");
  });

  it("cannot buy or sell after graduation", async function () {
    const { curve, trader, token } = await deployCurve();
    await curve.connect(trader).buy(0, { value: ethers.parseEther("2.2") });
    expect(await curve.graduated()).to.equal(true);
    await expect(curve.connect(trader).buy(0, { value: 1n })).to.be.revertedWith("curve has graduated");
    await token.connect(trader).approve(await curve.getAddress(), 1n);
    await expect(curve.connect(trader).sell(1n, 0)).to.be.revertedWith("curve has graduated");
  });

  it("burns LP to dead on graduation — curve holds zero leftover ETH seed", async function () {
    const { curve, trader, router } = await deployCurve();
    await curve.connect(trader).buy(0, { value: ethers.parseEther("2.2") });
    expect(await curve.graduated()).to.equal(true);
    expect(await curve.realEth()).to.equal(0n);
    const lp = await router.lp();
    const LP = await ethers.getContractAt("MockLP", lp);
    const deadBal = await LP.balanceOf("0x000000000000000000000000000000000000dead");
    expect(deadBal).to.be.gt(0n);
  });

  it("splits fees 70/30 platform/creator on every buy", async function () {
    const { curve, trader, treasury, creator } = await deployCurve();
    const beforeT = await ethers.provider.getBalance(treasury.address);
    const beforeC = await ethers.provider.getBalance(creator.address);
    const spent = ethers.parseEther("1");
    await curve.connect(trader).buy(0, { value: spent });
    const fee = spent * 100n / 10_000n;
    const plat = fee * 7000n / 10_000n;
    const rest = fee - plat;
    const afterT = await ethers.provider.getBalance(treasury.address);
    const afterC = await ethers.provider.getBalance(creator.address);
    expect(afterT - beforeT).to.equal(plat);
    expect(afterC - beforeC).to.equal(rest);
  });

  it("reentrancy: a malicious treasury cannot drain the curve", async function () {
    const [creator, trader] = await ethers.getSigners();
    const Router = await ethers.getContractFactory("MockUniswapV2Router");
    const router = await Router.deploy();
    await router.waitForDeployment();
    const Attacker = await ethers.getContractFactory("ReenteringTreasury");
    const attacker = await Attacker.deploy();
    await attacker.waitForDeployment();
    const Factory = await ethers.getContractFactory("BondingCurveFactory");
    const factory = await Factory.deploy(await router.getAddress(), await attacker.getAddress());
    await factory.waitForDeployment();
    const tx = await factory.connect(creator).launch(
      "Safe",
      "SAFE",
      1_000_000n * 10n ** 18n,
      ethers.parseEther("10"),
      ethers.parseEther("1"),
      800_000n * 10n ** 18n
    );
    const rc = await tx.wait();
    const ev = rc.logs.find((l) => l.fragment && l.fragment.name === "CurveLaunched");
    const Curve = await ethers.getContractFactory("BondingCurve");
    const curve = Curve.attach(ev.args.curve);
    await attacker.setCurve(await curve.getAddress());
    await trader.sendTransaction({ to: await attacker.getAddress(), value: ethers.parseEther("0.05") });
    await expect(curve.connect(trader).buy(0, { value: ethers.parseEther("0.3") })).to.be.reverted;
  });
});
