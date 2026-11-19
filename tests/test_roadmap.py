from av_control.center.roadmap import load_map


def test_grid_map(grid_net):
    m = load_map(grid_net)
    assert m.bounds == (0.0, 0.0, 800.0, 800.0)
    assert len(m.roads) == 80  # 방향별 도로, 교차로 안의 연결 차로는 빼고
    assert all(r.lanes and len(r.lanes[0]) >= 2 for r in m.roads)
    assert len(m.junctions) == 25
    assert sum(j.traffic_light for j in m.junctions) == 9  # 사거리에만 신호등
    assert set(m.to_dict()) == {"bounds", "roads", "junctions"}
