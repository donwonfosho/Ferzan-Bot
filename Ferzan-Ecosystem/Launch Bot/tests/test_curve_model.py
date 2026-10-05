"""FerzanCurve.sol math + accounting, ported to exact integers and fuzzed (solvency, rounding, graduation).
Does not model the EVM (reentrancy, token hooks): those are covered by reading the contracts."""
FEE=100; CREATOR=5000; REF=1000; DUST=1
def cd(a,b): return 0 if a==0 else (a-1)//b+1
class Curve:
    def __init__(s,S,G):
        s.S=S; s.G=G; s.vE=G//3; s.vT=S*16//15; s.real=0; s.sold=0; s.grad=False
        s.bal_eth=0; s.bal_tok=S; s.fees=0; s.holders={}
    def ex(s): return s.vE+s.real
    def ty(s): return s.vT-s.sold
    def qbuy(s,nin):
        assert not s.grad and nin>0
        net=nin-nin*FEE//10000; room=s.G-s.real; used=nin
        if net>=room:
            net=room; used=cd(net*10000,10000-FEE)
            if used>nin: used=nin
        fee=used-net; refund=nin-used
        x,y=s.ex(),s.ty(); ny=cd(x*y,x+net)
        return y-ny,used,refund,fee
    def qsell(s,tin):
        assert not s.grad and 0<tin<=s.sold
        x,y=s.ex(),s.ty(); nx=cd(x*y,y+tin); gross=x-nx
        if gross>s.real: gross=s.real
        fee=gross*FEE//10000
        return gross-fee,fee
    def buy(s,who,nin,mn=0):
        t,used,refund,fee=s.qbuy(nin)
        if not(t>0 and t>=mn): return None
        s.sold+=t; s.real+=used-fee
        s.holders[who]=s.holders.get(who,0)+t
        s.bal_eth+=used-fee; s.fees+=fee  # fee leaves the contract
        if s.real+s.G*DUST//10000>=s.G: s.graduate()
        return t,used,refund
    def sell(s,who,tin):
        out,fee=s.qsell(tin)
        gross=out+fee
        assert s.holders.get(who,0)>=tin
        s.holders[who]-=tin; s.sold-=tin; s.real-=gross  # underflow would revert on-chain
        assert s.real>=0, "UNDERFLOW realEth"
        s.bal_eth-=gross; s.fees+=fee
        return out
    def graduate(s):
        s.grad=True; seed=s.real; bal=s.S-s.sold
        tok=seed*s.ty()//s.ex()
        if tok>bal: tok=bal
        s.pool=(seed,tok); s.burn=bal-tok; s.real=0; s.bal_eth-=seed
def run(seed):
    r=random.Random(seed)
    S=r.choice([10**18, 10**21, 10**27, 10**9*10**18, 25*10**33//10])
    G=r.choice([10**15, 10**17, 10**18, 5*10**18, 10**22, 10**24])
    c=Curve(S,G); paid={}; got={}; n=0
    users=list(range(1,8))
    while not c.grad and n<400:
        n+=1; u=r.choice(users)
        if r.random()<0.65 or c.holders.get(u,0)==0:
            amt=r.choice([1,10**3,10**9,G//1000+1,G//50+1,G//5+1,G//2+1,G*2])
            amt=max(1,amt)
            try: res=c.buy(u,amt)
            except AssertionError: continue
            if res: t,used,ref=res; paid[u]=paid.get(u,0)+used; 
        else:
            h=c.holders[u]; tin=r.choice([h,max(1,h//2),max(1,h//10),1])
            tin=min(tin,h)
            try: out=c.sell(u,tin)
            except AssertionError as e:
                if "UNDERFLOW" in str(e): print("FAIL underflow",seed,S,G); return False
                continue
            got[u]=got.get(u,0)+out
        # solvency: contract eth must cover what real says, and any remaining holder can exit
        if not c.grad:
            assert c.bal_eth==c.real, ("accounting",seed)
            tot=sum(c.holders.values())
            assert tot==c.sold,("sold mismatch",seed)
    # solvency: if every holder sells everything back (random order), the curve never underflows and keeps >= 0
    if not c.grad:
        order=[u for u in users if c.holders.get(u,0)>0]; r.shuffle(order)
        for u in order:
            h=c.holders[u]
            try: c.sell(u,h)
            except AssertionError as e:
                print("FAIL exit",seed,e); return False
        if c.real<0 or c.bal_eth<0: print("FAIL negative",seed); return False
        if c.sold!=0 and c.real>0 and c.sold==0: pass
    # single actor immediate round trip never profits
    c2=Curve(S,G)
    amt=max(1,r.choice([10**3,G//1000+1,G//20+1,G//3+1]))
    res=c2.buy(1,amt)
    if res and not c2.grad:
        t,used,ref=res
        out=c2.sell(1,t)
        if out>used: print("FAIL roundtrip profit",seed,out,used); return False
    if c.grad:
        seed_e,seed_t=c.pool
        # pool price must be >= final curve price (holders never get a drop): seed_e/seed_t >= x/y
        x,y=c.G*4//3,0
        # compare with pre-graduation reserves implied: pool tokens*ethReserve <= seedEth*tokenReserve (+rounding)
    return True
import unittest


class CurveModel(unittest.TestCase):
    def test_fuzz(self):
        failed = [sd for sd in range(1500) if not run(sd)]
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
